"""GUI web backend — the seam that fails if sync→async streaming breaks.

Ponytail: one check per contract (event map, chat stream, nucleate, reset, stop,
messages). No browser e2e in v1. Skipped whole if fastapi isn't installed.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.link_cases import URL_CASES

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from silica.agent.events import (  # noqa: E402
    BatchRunStartEvent,
    LLMStreamEvent,
    ReasoningEvent,
    ToolCompleteEvent,
    ToolErrorEvent,
    ToolStartEvent,
)


def _read_sse(response) -> list[dict]:
    return [
        json.loads(line[len("data: ") :])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


@pytest.fixture
def client(tmp_vault, tmp_path, monkeypatch):
    """Fresh module-level session per test, backed by a tmp fs vault."""
    from silica.ui.web import server

    monkeypatch.setattr(server, "SESSIONS_DIR", tmp_path / "web_sessions")
    server._reset_session()
    return TestClient(server.app), server


def test_event_to_json_maps_the_render_event_seam():
    from silica.ui.web.callback import event_to_json

    assert event_to_json(LLMStreamEvent("content", "hi", 0)) == {
        "type": "delta",
        "kind": "content",
        "text": "hi",
    }
    assert event_to_json(ToolStartEvent("t", {}, "c1", 0)) == {
        "type": "tool_start",
        "name": "t",
        "id": "c1",
        "target": "",
        "effect": "read",
        "notes": [],
    }
    # note refs are pulled from the tool args (allowlisted keys) → sources chips
    assert event_to_json(ToolStartEvent("t", {"path": "a/b.md"}, "c2", 0)) == {
        "type": "tool_start",
        "name": "t",
        "id": "c2",
        "target": "",  # unknown tool: no table entry, so no named target
        "effect": "read",
        "notes": ["a/b.md"],
    }
    # a write names the file it touched, and the footer can group it apart from reads
    assert event_to_json(ToolStartEvent("silica_write_note", {"path": "a/b.md"}, "c3", 0)) == {
        "type": "tool_start",
        "name": "write note",
        "id": "c3",
        "target": "a/b.md",
        "effect": "written",
        "notes": ["a/b.md"],
    }
    # a move leaves the note at `to`: that is the ref the chip can open
    assert event_to_json(ToolStartEvent("silica_move", {"ref": "a.md", "to": "b.md"}, "c4", 0)) == {
        "type": "tool_start",
        "name": "move",
        "id": "c4",
        "target": "a.md → b.md",
        "effect": "moved",
        "notes": ["b.md"],
    }
    assert event_to_json(ToolCompleteEvent("t", {}, "c1", "ok", 0.1, 0)) == {
        "type": "tool_done",
        "name": "t",
        "id": "c1",
    }
    assert event_to_json(ToolErrorEvent("t", "c1", "boom", 0)) == {
        "type": "tool_error",
        "name": "t",
        "id": "c1",
        "error": "boom",
    }
    assert event_to_json(BatchRunStartEvent("r", "refine", "X", 3)) == {
        "type": "batch",
        "kind": "refine",
        "label": "X",
    }
    # v1 ignores reasoning/thinking events (no JSON emitted).
    assert event_to_json(ReasoningEvent("thinking", 0)) is None


def test_injector_start_carries_the_file_and_the_tracks():
    """Regression: _tool_target read the legacy `inbox_file` key while every real
    caller passes `inbox_files`, so a nucleate run showed a bare "injector" with
    no document name at all. The two phase tracks ride along so the client can
    draw the pipeline greyed out instead of growing it a row at a time."""
    from silica.agent.events import ToolStartEvent
    from silica.ui.web.callback import event_to_json

    ev = event_to_json(ToolStartEvent(
        "silica_run_injector", {"inbox_files": ["Inbox/a.md", "Inbox/b.md"]}, "c9", 0))
    assert ev["target"] == "Inbox/a.md +1"
    assert ev["pipeline"]["file"][0] == "recon"
    assert ev["pipeline"]["chunk"][0] == "collision"
    # An exception branch is not a step: listing it made every healthy run show a
    # pending rollback that was never coming.
    assert "rollback" not in ev["pipeline"]["file"] + ev["pipeline"]["chunk"]


def test_phase_event_restates_the_whole_position():
    from silica.agent.events import PhaseEvent
    from silica.ui.web.callback import event_to_json

    assert event_to_json(PhaseEvent(
        phase="distill", status="running", scope="chunk", file_idx=1, file_total=2,
        chunk_idx=2, chunk_total=5, source_file="b.md")) == {
        "type": "phase", "phase": "distill", "status": "running", "scope": "chunk",
        "source_file": "b.md", "file_idx": 1, "file_total": 2,
        "chunk_idx": 2, "chunk_total": 5,
    }


def test_phase_wire_sends_labels_the_track_can_match():
    """Found in the browser, not in the code: the client matches rows by exact
    string, and a phase whose id differs from its label (hub_update -> hub-update)
    needs its own rule, so any guess the client makes leaves it grey for the whole
    run. The server maps."""
    from silica.agent.events import PhaseEvent
    from silica.ui.web.callback import _PHASE_TRACKS, event_to_json

    every = _PHASE_TRACKS["file"] + _PHASE_TRACKS["chunk"]
    for pid in ["recon", "payload", "salience", "distill", "hub_update", "lint"]:
        wire = event_to_json(PhaseEvent(phase=pid, status="running", scope="chunk",
                                        file_idx=0, file_total=1, chunk_idx=0, chunk_total=1))
        assert wire["phase"] in every, f"{pid} arrives as {wire['phase']!r}, which matches no row"


def test_injector_summary_reads_the_five_terminal_statuses():
    """no_ops and already_nucleated finish WELL while writing nothing; rendering
    them as a bare success with zero counts is true and unreadable."""
    import json
    from silica.ui.web.callback import _injector_summary

    def s(**kw):
        return _injector_summary(json.dumps(kw))

    # "Success" is capitalised at the source (states/finalize.py) and the rest
    # are not; normalising here keeps cli.py's _DRAIN_SETTLED on its contract.
    ok = s(final_status="Success", yield_notes=14, yield_links=9, files_total=2)
    assert (ok["kind"], ok["notes"], ok["links"], ok["files"]) == ("ok", 14, 9, 2)

    assert s(final_status="no_ops")["reason"] == "no operations produced"
    assert s(final_status="already_nucleated")["reason"] == "already in the vault"
    assert s(final_status="no_ops")["kind"] == "empty"

    part = s(final_status="partial", chunks_committed=5,
             failed_chunks=[{"chunk": "f0_c3", "phase": "lint", "error": "…"}])
    assert part["kind"] == "partial"
    assert part["failed_chunks"] == [{"chunk": "f0_c3", "phase": "lint"}]

    # Unparseable / absent result must not read as a success.
    assert _injector_summary(None)["kind"] == "failed"
    assert _injector_summary("not json")["kind"] == "failed"


def test_transcript_replay_restates_the_injector_outcome():
    """Regression: /messages skipped every role=="tool" message, so the stored
    result never reached the replay and a reloaded chat could only say the
    injector had run — not what it wrote or which chunks died."""
    import json
    from silica.ui.web.callback import tool_calls_to_json

    msg = {"role": "assistant", "tool_calls": [{
        "id": "c1",
        "function": {"name": "silica_run_injector",
                     "arguments": json.dumps({"inbox_files": ["Inbox/a.md"]})},
    }]}
    results = {"c1": json.dumps({"final_status": "Success", "yield_notes": 3,
                                 "yield_links": 4, "files_total": 1})}

    assert "summary" not in tool_calls_to_json(msg, None, None)[0]
    line = tool_calls_to_json(msg, None, results)[0]
    assert line["target"] == "Inbox/a.md"
    assert (line["summary"]["notes"], line["summary"]["links"]) == (3, 4)


def test_index_cache_busts_churning_assets(client):
    # app.js/app.css must carry a ?v= content hash so an edited asset can't be
    # served stale from the browser's heuristic cache; vendored bundles don't.
    tc, _ = client
    html = tc.get("/").text
    import re

    assert re.search(r"/static/app\.js\?v=[0-9a-f]{8}", html), "app.js not cache-busted"
    assert re.search(r"/static/app\.css\?v=[0-9a-f]{8}", html), "app.css not cache-busted"
    assert "/static/app.js\"" not in html, "unversioned app.js reference still present"


def test_quick_action_segments_name_real_commands():
    """The launch pad prefills the composer, so a stale segment would hand the
    user a command the turn answers 'not available' to."""
    import re

    from silica.ui.commands import COMMANDS
    from silica.ui.web import server

    html = (Path(server.__file__).parent / "static" / "index.html").read_text()
    pad = html.split('id="quick-actions"')[1].split("</div>")[0]
    offered = set(re.findall(r'data-action="(/[a-z-]+)"', pad))
    live = {c.name for c in COMMANDS if not c.repl_only}
    assert offered, "no command segments found in the quick-action pad"
    assert offered <= live, f"quick actions offer dead commands: {offered - live}"


def _repl_dispatched_commands() -> set[str]:
    """Command names the REPL's three dispatchers recognise, read off their source.

    ponytail: a source-level lint, because the direct handler does the work inline
    (calling it to ask "do you know /embed?" would rebuild an index). It breaks if
    the dispatch stops being a `cmd == "/x"` chain — rewrite it as a dict then, and
    read the keys.
    """
    import inspect
    import re

    from silica.cli import (
        _REFRESH,
        _expand_web_turn,
        _expand_workflow_shortcut,
        _handle_direct_shortcut,
    )

    src = inspect.getsource(_handle_direct_shortcut) + inspect.getsource(_expand_workflow_shortcut)
    return set(re.findall(r'cmd (?:==|in \()\s*"(/[a-z-]+)"', src)) | set(
        re.findall(r'"(/[a-z-]+)"\)', src)
    ) | set(_REFRESH) | set(  # the three index refreshes dispatch off a dict
        # The web-escalation expander matches on parts[0], not on a `cmd ==`
        # chain, and names only the commands it owns: take every literal.
        re.findall(r'"(/[a-z-]+)"', inspect.getsource(_expand_web_turn))
    )


def test_every_advertised_command_is_dispatchable_by_the_gui():
    """The GUI's picker must not offer what the chat turn answers 'not available'
    to. This is the drift that shipped: the web kept its own hand-written list of
    direct commands, and /lexical /wiki /graph /map /find /vault were never on it.
    """
    from silica.ui.commands import COMMANDS

    dispatched = _repl_dispatched_commands()
    orphans = [c.name for c in COMMANDS if not c.repl_only and c.name not in dispatched]
    assert not orphans, f"advertised in the GUI but no dispatcher handles them: {orphans}"


def test_commands_endpoint_hides_repl_only_commands(client):
    tc, _ = client
    from silica.ui.commands import COMMANDS

    offered = {c["name"] for c in tc.get("/commands").json()}
    assert "/exit" not in offered and "/help" not in offered
    assert offered == {c.name for c in COMMANDS if not c.repl_only}


def test_health_reports_only_what_needs_fixing(client, monkeypatch):
    """A down embedder must reach the browser; a green check must not toast."""
    tc, _ = client
    import silica.onboarding.checks as checks

    monkeypatch.setattr(checks, "run_checks", lambda cfg: [
        checks.CheckResult("chat model", "ok", "fine"),
        checks.CheckResult("embeddings", "warn", "http://x unreachable", "start it"),
    ])
    assert tc.get("/health").json() == [
        {"name": "embeddings", "status": "warn", "detail": "http://x unreachable", "hint": "start it"}
    ]


def test_direct_command_runs_without_an_llm_round_trip(client, monkeypatch):
    """/plans is REPL-direct: the GUI must run it inline, not hand it to the agent."""
    tc, server = client

    def boom(*a, **kw):
        raise AssertionError("a direct command must not reach the agent")

    monkeypatch.setattr(server, "run_agent", boom)

    events = _read_sse(tc.post("/chat", json={"text": "/plans"}))
    assert events[-1]["type"] == "done"
    assert [m["content"] for m in server.messages if m["role"] == "user"] == ["/plans"]


def test_declined_direct_command_leaves_no_duplicate_user_turn(client, monkeypatch):
    """Every slash command is offered to the direct handler first; one it declines
    must fall through with exactly ONE user turn in history — the expanded one."""
    tc, server = client

    def fake_run_agent(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
        messages.append({"role": "assistant", "content": "ok"})
        return "ok"

    monkeypatch.setattr(server, "run_agent", fake_run_agent)

    tc.post("/chat", json={"text": "/summarize Concepts/RAG.md"})
    users = [m["content"] for m in server.messages if m["role"] == "user"]
    assert len(users) == 1, f"duplicated user turn: {users}"
    assert users[0] != "/summarize Concepts/RAG.md", "the agent got the raw command, not the expansion"


def test_chat_streams_events_and_appends_the_user_message(client, monkeypatch):
    tc, server = client

    def fake_run_agent(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
        tool_progress_callback(ToolStartEvent("silica_x", {}, "c1", 0))
        tool_progress_callback(LLMStreamEvent("content", "Hello", 0))
        tool_progress_callback(ToolCompleteEvent("silica_x", {}, "c1", "ok", 0.0, 0))
        messages.append({"role": "assistant", "content": "Hello"})
        return "Hello"

    monkeypatch.setattr(server, "run_agent", fake_run_agent)

    resp = tc.post("/chat", json={"text": "hi there"})
    assert resp.status_code == 200
    events = _read_sse(resp)
    types = [e["type"] for e in events]
    assert "tool_start" in types
    assert "delta" in types
    assert types[-1] == "done"
    assert events[-1]["answer"] == "Hello"
    assert any(m["role"] == "user" and m["content"] == "hi there" for m in server.messages)


def test_inline_slash_command_reports_its_own_result_not_an_error(client, monkeypatch):
    """/fetch (like /web-search and /convert) does the whole job inside the
    workflow expansion and returns "" — the REPL's "nothing left for the agent"
    sentinel. The GUI read that "" as "not available in this session", so the
    browser was told the command failed while the note was already on disk and
    the success line had gone to the server's own stdout."""
    tc, server = client
    import silica.sources.web_research as wr

    monkeypatch.setattr(wr, "fetch_to_inbox", lambda url: "Inbox/Example Domain.md")

    events = _read_sse(tc.post("/chat", json={"text": "/fetch https://example.test/"}))
    done = events[-1]
    assert done["type"] == "done"
    assert "Inbox/Example Domain.md" in done["answer"]


def test_inline_slash_command_reports_its_failure_too(client, monkeypatch):
    """Truthfully: a fetch that raises must surface as the failure it was, not
    as a silent success and not as 'not available in this session'."""
    tc, server = client
    import silica.sources.web_research as wr

    def boom(url):
        raise ValueError("403 at https://example.test/: bot wall")

    monkeypatch.setattr(wr, "fetch_to_inbox", boom)

    events = _read_sse(tc.post("/chat", json={"text": "/fetch https://example.test/"}))
    done = events[-1]
    assert done["type"] == "done"
    assert "fetch failed" in done["answer"] and "bot wall" in done["answer"]


def test_web_routes_as_an_agent_turn_with_trace_built_citations(client, monkeypatch):
    """/web is NOT a direct command: it runs the agent with web-only tools, so the
    answer arrives as markdown (not a fenced text block) and carries the Sources
    block built from the trace. A direct handler here would append the captured
    answer a second time."""
    tc, server = client

    def fake_run_agent(messages, model, tool_progress_callback=None, cancel_token=None,
                       constraints=None, **kw):
        assert constraints.tools == (
            "web_search", "web_fetch", "remember", "find_in_page"
        )
        tool_progress_callback(ToolCompleteEvent(
            name="web_search", args={"query": "q"}, call_id="c1",
            result=json.dumps([{"title": "Rewiring", "url": "https://a.test/rw"}]),
            duration_s=0.0, iteration=1,
        ))
        messages.append({"role": "assistant", "content": "From the web: it swaps edges."})
        return "From the web: it swaps edges."

    monkeypatch.setattr(server, "run_agent", fake_run_agent)

    done = _read_sse(tc.post("/chat", json={"text": "/web graph rewiring"}))[-1]
    assert done["type"] == "done"
    assert "```text" not in done["answer"]  # not the direct-command wrapper
    assert "## Sources (web)" in done["answer"]
    assert "https://a.test/rw" in done["answer"]
    # history carries what the user saw
    assert server.messages[-1]["content"] == done["answer"]


def test_bare_web_without_a_question_yields_one_usage_error(client):
    tc, _ = client
    events = _read_sse(tc.post("/chat", json={"text": "/web"}))
    assert events[-1]["type"] == "error"
    assert "Usage: /web" in events[-1]["error"]


def test_done_carries_the_hint_when_every_recall_missed(client, monkeypatch):
    """The thin-coverage hint is an optional field on the existing done event."""
    tc, server = client
    from silica.agent.recall_watch import THIN_COVERAGE_HINT

    def fake_run_agent(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
        tool_progress_callback(ToolCompleteEvent(
            name="silica_recall", args={}, call_id="c1",
            result=json.dumps({"notes": [], "facts": 0}), duration_s=0.0, iteration=1,
        ))
        messages.append({"role": "assistant", "content": "I have nothing on that."})
        return "I have nothing on that."

    monkeypatch.setattr(server, "run_agent", fake_run_agent)

    done = _read_sse(tc.post("/chat", json={"text": "what is graph rewiring?"}))[-1]
    assert done["hint"] == THIN_COVERAGE_HINT


def test_a_turn_that_found_notes_carries_no_hint(client, monkeypatch):
    tc, server = client

    def fake_run_agent(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
        tool_progress_callback(ToolCompleteEvent(
            name="silica_recall", args={}, call_id="c1",
            result=json.dumps({"notes": ["Concepts/RAG.md"]}), duration_s=0.0, iteration=1,
        ))
        messages.append({"role": "assistant", "content": "You wrote about it."})
        return "You wrote about it."

    monkeypatch.setattr(server, "run_agent", fake_run_agent)

    done = _read_sse(tc.post("/chat", json={"text": "what about RAG?"}))[-1]
    assert "hint" not in done


def test_an_unknown_slash_command_is_still_reported_as_unavailable(client):
    """The `None` verdict (no dispatcher recognises it) must keep its error."""
    tc, _ = client
    events = _read_sse(tc.post("/chat", json={"text": "/definitely-not-a-command"}))
    assert events[-1]["type"] == "error"
    assert "not available in this session" in events[-1]["error"]


def test_run_turn_yields_raw_dicts_not_sse_frames(client, monkeypatch):
    """The transport-neutral core: raw wire dicts, no `data: ` framing, ending
    in one `done` dict. This is what both `--gui` (SSE) and `connect` (WS) wrap."""
    tc, server = client

    def fake_run_agent(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
        tool_progress_callback(LLMStreamEvent("text", "Hi", 0))
        messages.append({"role": "assistant", "content": "Hi"})
        return "Hi"

    monkeypatch.setattr(server, "run_agent", fake_run_agent)

    async def collect():
        return [item async for item in server.run_turn("hello")]

    items = asyncio.run(collect())
    assert all(isinstance(i, dict) for i in items)  # dicts, not SSE strings
    assert any(i["type"] == "delta" and i["text"] == "Hi" for i in items[:-1])
    assert items[-1]["type"] == "done"
    assert items[-1]["answer"] == "Hi"
    assert any(m["role"] == "user" and m["content"] == "hello" for m in server.messages)
    assert server._busy is False  # gate freed on normal completion


def test_run_turn_error_path_yields_one_error_and_frees_the_gate(client, monkeypatch):
    """A worker crash ends the stream with exactly one `error` dict, and the
    busy-gate is freed (never leave the UI stuck, never wedge the next turn)."""
    tc, server = client

    def boom(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(server, "run_agent", boom)

    async def collect():
        return [item async for item in server.run_turn("hi")]

    items = asyncio.run(collect())
    assert sum(1 for i in items if i["type"] == "error") == 1
    assert items[-1]["type"] == "error"
    assert "kaboom" in items[-1]["error"]
    assert server._busy is False


def test_run_turn_abandonment_holds_gate_until_worker_exits(client, monkeypatch):
    """Consumer stops iterating mid-stream (dropped SSE/WS client): the worker
    is a zombie until it observes the cancel. The gate MUST stay closed until it
    actually exits, or a second turn mutates `messages` concurrently."""
    import threading
    import time

    tc, server = client
    started = threading.Event()

    def slow(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
        tool_progress_callback(LLMStreamEvent("text", "partial", 0))
        started.set()
        deadline = time.monotonic() + 3.0  # bounded so a broken fix FAILS, never hangs
        while (cancel_token is None or not cancel_token.is_set()) and time.monotonic() < deadline:
            time.sleep(0.005)  # spin until cancelled — the abandonment signal
        messages.append({"role": "assistant", "content": "partial"})
        return "partial"

    monkeypatch.setattr(server, "run_agent", slow)

    async def scenario():
        gen = server.run_turn("hi")
        first = await gen.__anext__()  # one delta, then abandon
        assert first["type"] == "delta"
        await asyncio.to_thread(started.wait, 1.0)
        await gen.aclose()  # GeneratorExit into run_turn

        # zombie still alive → gate closed, cancel signalled
        assert server._busy is True
        assert server.current_cancel is not None and server.current_cancel.is_set()

        # once the worker sees the cancel and exits, its done-callback frees the gate
        for _ in range(400):
            if not server._busy:
                break
            await asyncio.sleep(0.005)
        assert server._busy is False

    asyncio.run(scenario())


def test_sweep_frees_the_gate_when_no_worker_ever_started(client):
    """Never-iterated generator (client drops between POST and first __anext__):
    run_turn never runs, so the SSE background sweep frees the eagerly-claimed
    gate. Guards against a permanently 409-locked server."""
    tc, server = client
    assert server._begin_turn() is True
    assert server._busy is True
    server.current_task = None  # no worker was created
    server._sweep_if_orphaned()
    assert server._busy is False


def test_nucleate_stages_uploads_and_hands_files_to_the_agent(client, monkeypatch):
    tc, server = client

    ran: dict = {}

    def fake_run_agent(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
        ran["msgs"] = list(messages)
        messages.append({"role": "assistant", "content": "ok"})
        return "ok"

    monkeypatch.setattr(server, "run_agent", fake_run_agent)

    resp = tc.post(
        "/nucleate",
        files=[("files", ("note.md", b"# Hi\n\nsome body text to stage", "text/markdown"))],
        data={"text": "file these under Concepts/AI"},
    )
    assert resp.status_code == 200

    from silica.config import CONFIG

    saved = Path(CONFIG.vault_path) / "Inbox" / "note.md"
    assert saved.exists()  # upload landed in the inbox (not nucleated yet)
    # the agent turn carries the user's instruction *and* the staged file path
    user = next(m for m in ran["msgs"] if m["role"] == "user")
    assert "file these under Concepts/AI" in user["content"]
    assert "Inbox/note.md" in user["content"]


def test_compose_nucleate_turn_defaults_empty_text_and_lists_files():
    from silica.ui.web.server import _compose_nucleate_turn

    # empty instruction → default nucleate ask; markdown vs code stubs both listed
    msg = _compose_nucleate_turn("", ["Inbox/a.md"], ["Code/b.md"])
    assert "Nucleate the attached file(s)" in msg
    assert "Inbox/a.md" in msg and "Code/b.md" in msg

    # a real instruction is kept verbatim as the turn's lead
    msg2 = _compose_nucleate_turn("summarize these", ["Inbox/a.md"], [])
    assert msg2.startswith("summarize these")
    assert "Inbox/a.md" in msg2


def test_reset_restores_a_fresh_session(client, monkeypatch):
    tc, server = client

    def fake_run_agent(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
        messages.append({"role": "assistant", "content": "a"})
        return "a"

    monkeypatch.setattr(server, "run_agent", fake_run_agent)

    tc.post("/chat", json={"text": "hi"})
    assert any(m["role"] == "user" for m in server.messages)

    r = tc.post("/reset")
    assert r.status_code == 200
    assert not any(m["role"] in ("user", "assistant") for m in server.messages)


def test_stop_signals_the_in_flight_cancel_token(client):
    tc, server = client
    import threading

    server.current_cancel = threading.Event()
    r = tc.post("/stop")
    assert r.status_code == 200
    assert server.current_cancel.is_set()


def test_messages_endpoint_returns_user_and_assistant_turns(client, monkeypatch):
    tc, server = client

    def fake_run_agent(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
        messages.append({"role": "assistant", "content": "Reply"})
        return "Reply"

    monkeypatch.setattr(server, "run_agent", fake_run_agent)

    tc.post("/chat", json={"text": "question"})
    data = tc.get("/messages").json()
    roles = [m["role"] for m in data]
    assert "user" in roles and "assistant" in roles
    assert not any(m["role"] == "system" for m in data)


def test_messages_endpoint_replays_the_tool_calls_of_a_turn(client):
    """Reopening a chat has to show the steps, and a failed one as failed."""
    tc, server = client
    call = lambda cid, name, args: {  # noqa: E731
        "id": cid, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }
    server.messages += [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "Let me look.", "silica_reasoning": "the vault may hold it",
         "tool_calls": [call("c1", "silica_read_note", {"name": "a.md"}),
                        call("c2", "silica_write_note", {"path": "b.md"})]},
        {"role": "tool", "tool_call_id": "c1", "content": "body"},
        {"role": "tool", "tool_call_id": "c2", "content": '{"error": "gate refused"}'},
        {"role": "assistant", "content": "Done."},
    ]
    data = [m for m in tc.get("/messages").json() if m["role"] == "assistant"]
    assert [t["target"] for t in data[0]["tools"]] == ["a.md", "b.md"]
    assert [t["error"] for t in data[0]["tools"]] == [False, True]
    # the chain of thought is replayable, step by step
    assert data[0]["thinking"] == "the vault may hold it"
    assert data[1]["thinking"] == ""
    # both halves of the one reply survive, in order, for the client to merge
    assert [m["content"] for m in data] == ["Let me look.", "Done."]


def test_sessions_persist_across_reset_and_reload(client, monkeypatch):
    tc, server = client

    def fake_run_agent(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
        messages.append({"role": "assistant", "content": "Reply one"})
        return "Reply one"

    monkeypatch.setattr(server, "run_agent", fake_run_agent)

    tc.post("/chat", json={"text": "first question"})
    listed = tc.get("/sessions")
    sessions = listed.json()
    assert len(sessions) == 1
    assert sessions[0]["title"] == "first question"
    sid = sessions[0]["id"]
    assert listed.headers["X-Silica-Session"] == sid

    # new chat clears the live session; the saved one survives on disk
    tc.post("/reset")
    assert not any(m["role"] in ("user", "assistant") for m in server.messages)

    r = tc.post("/session/load", json={"id": sid})
    assert r.status_code == 200
    assert any(m.get("content") == "Reply one" for m in server.messages)
    assert server.current_session_id == sid

    # unknown / path-traversal ids are rejected
    assert tc.post("/session/load", json={"id": "../../etc/passwd"}).status_code == 404
    assert tc.post("/session/load", json={"id": "deadbeef"}).status_code == 404


# ---------------------------------------------------------------------------
# _linkify — resolvable note refs become .note-link anchors (token-stream, so
# code is never touched). Pure: driven by a fake dict resolver, no vault.
# ---------------------------------------------------------------------------

_FAKE_INDEX = {
    "Foo": "Foo.md",
    "a/b": "sub/a-b.md",
    "concepts/mind-maps.md": "concepts/mind-maps.md",
    "concepts/x.md": "concepts/x.md",
    "index": "index.md",  # resolvable, but not path-shaped → must NOT link
}


def _fake_resolve(ref: str):
    return _FAKE_INDEX.get(ref)


def test_linkify_resolved_wikilink_becomes_clean_anchor():
    from silica.ui.web.server import _linkify

    html = _linkify("see [[Foo]] here", _fake_resolve)
    assert '<a class="note-link" data-path="Foo.md">Foo</a>' in html
    assert "[[" not in html and "]]" not in html


def test_linkify_wikilink_alias_shows_alias_but_resolves_target():
    from silica.ui.web.server import _linkify

    html = _linkify("read [[a/b|Bar]] now", _fake_resolve)
    assert 'data-path="sub/a-b.md"' in html
    assert ">Bar</a>" in html


def test_linkify_unresolved_wikilink_renders_as_broken_anchor():
    from silica.ui.web.server import _linkify

    html = _linkify("a [[nope]] ref", _fake_resolve)
    assert '<a class="note-link broken">nope</a>' in html
    assert "data-path" not in html  # click is a no-op by construction
    assert "[[" not in html


def test_linkify_pathlike_md_token_becomes_link_with_clean_name():
    from silica.ui.web.server import _linkify

    html = _linkify("open concepts/mind-maps.md today", _fake_resolve)
    assert 'data-path="concepts/mind-maps.md"' in html
    assert ">mind-maps</a>" in html


def test_linkify_bare_word_is_never_linked():
    from silica.ui.web.server import _linkify

    # `index` resolves in the fake index, but has no `/` and no `.md` → not a
    # link candidate, so predictability wins over resolvability.
    html = _linkify("the index of notes", _fake_resolve)
    assert "note-link" not in html


def test_linkify_never_touches_code():
    from silica.ui.web.server import _linkify

    html = _linkify("run `concepts/x.md` inline", _fake_resolve)
    assert "note-link" not in html
    assert "<code>concepts/x.md</code>" in html


def test_linkify_without_resolver_is_plain_render():
    from silica.ui.web.server import _linkify

    assert _linkify("see [[Foo]] here").strip() == "<p>see [[Foo]] here</p>"


# ---------------------------------------------------------------------------
# _linkify — bare URLs. Shared corpus with the mdLite half (tests/link_cases.py).
# ---------------------------------------------------------------------------


def _basename_resolve(ref: str):
    """A resolver with `_resolve_in`'s basename fallback, which is the whole
    reason a URL used to come back as a note: it matches the last path segment,
    and `_PATHLIKE` hands it every token with a slash in it."""
    stem = ref.rsplit("/", 1)[-1].removesuffix(".md").lower()
    return {"chemistry": "areas/chemistry.md"}.get(stem)


@pytest.mark.parametrize("md, present, absent", URL_CASES)
def test_linkify_url_contract(md, present, absent):
    from silica.ui.web.server import _linkify

    html = _linkify(md, _fake_resolve)
    for frag in present:
        assert frag in html, html
    for frag in absent:
        assert frag not in html, html


def test_linkify_bare_url_is_not_swallowed_by_a_same_named_note():
    from silica.ui.web.server import _linkify

    # It used to render as `<a class="note-link" data-path="areas/chemistry.md">
    # chemistry</a>`: the URL disappeared from the page and the click opened an
    # unrelated note. Every web citation was one note name away from this.
    html = _linkify("vedi https://en.wikipedia.org/wiki/chemistry ok", _basename_resolve)
    assert '<a href="https://en.wikipedia.org/wiki/chemistry">' in html
    assert "note-link" not in html


def test_linkify_never_nests_a_note_ref_inside_a_link():
    from silica.ui.web.server import _linkify

    # The link text carries a path, so the note pass fired inside the anchor and
    # emitted <a> within <a>, which the browser then unnests.
    html = _linkify("[vedi chemistry.md](https://e.com)", _basename_resolve)
    assert html.count("<a ") == 1, html
    assert "note-link" not in html


def test_embed_with_subpath_fragment_still_renders_image():
    # Obsidian embeds carry a #center/#heading subpath and a width alias:
    # the fragment must not defeat the asset-extension check (regression).
    from silica.ui.web.server import _linkify

    html = _linkify("![[Pasted image 1.png#center|500]]", _fake_resolve)
    assert '<img src="/asset?path=Pasted%20image%201.png"' in html
    assert 'width="500"' in html
    assert "note-link broken" not in html


# ---------------------------------------------------------------------------
# OFM sugar — highlights, tags, callouts, tasks, mermaid, comments/block-ids,
# frontmatter. Same pure-resolver setup as the _linkify tests above.
# ---------------------------------------------------------------------------

def test_ofm_highlight_and_tag_render():
    from silica.ui.web.server import _linkify

    html = _linkify("a ==hot== take on #graph/theory", _fake_resolve)
    assert "<mark>hot</mark>" in html
    assert '<span class="tag">#graph/theory</span>' in html


def test_ofm_sugar_never_fires_in_code():
    from silica.ui.web.server import _linkify

    html = _linkify("run `#foo` now\n\n```\n#bar\n==nope==\n```", _fake_resolve)
    assert 'class="tag"' not in html
    assert "<mark>" not in html


def test_ofm_callout_gets_class_and_title():
    from silica.ui.web.server import _linkify

    html = _linkify("> [!warning] Watch out\n> the body", _fake_resolve)
    assert 'class="callout callout-warning"' in html
    assert '<p class="callout-title">Watch out</p>' in html
    assert "the body" in html
    assert "[!warning]" not in html


def test_ofm_plain_blockquote_is_untouched():
    from silica.ui.web.server import _linkify

    html = _linkify("> just a quote", _fake_resolve)
    assert "callout" not in html


def test_ofm_task_items_become_checkboxes():
    from silica.ui.web.server import _linkify

    html = _linkify("- [ ] open\n- [x] done", _fake_resolve)
    assert html.count('<input type="checkbox" disabled') == 2
    assert 'disabled checked' in html
    assert "[ ]" not in html and "[x]" not in html


def test_ofm_mermaid_fence_becomes_client_hook():
    from silica.ui.web.server import _linkify

    html = _linkify("```mermaid\ngraph TD; A-->B;\n```", _fake_resolve)
    assert '<pre class="mermaid">' in html
    assert "A--&gt;B" in html  # content is escaped, mermaid.js reads textContent
    assert "mermaid" not in _linkify("```python\nx = 1\n```", _fake_resolve)


def test_ofm_comments_and_block_ids_stripped():
    from silica.ui.web.server import _linkify

    html = _linkify("keep %%hidden%% this ^anchor-id\nnext line", _fake_resolve)
    assert "hidden" not in html
    assert "anchor-id" not in html
    assert "keep" in html and "next line" in html


def test_ofm_strip_spares_fenced_code():
    # %% and trailing ^ids inside a fence are code, not OFM sugar — and a
    # lone %% in a fence must not pair with a prose %% and swallow the block.
    from silica.ui.web.server import _linkify

    md = (
        "before %%gone%%\n\n"
        "```\n%% cell marker\nx = y ^2\n```\n\n"
        "after %%also gone%% end\n"
    )
    html = _linkify(md, _fake_resolve)
    assert "gone" not in html
    assert "%% cell marker" in html
    assert "x = y ^2" in html
    assert "before" in html and "after" in html and "end" in html


def test_ofm_image_embed_becomes_img_via_asset():
    from silica.ui.web.server import _linkify

    html = _linkify("see ![[img/pic 1.png]] and ![[shot.jpg|300]]", _fake_resolve)
    assert '<img src="/asset?path=img/pic%201.png" alt="pic 1">' in html
    assert '<img src="/asset?path=shot.jpg" alt="shot" width="300">' in html


def test_markdown_relative_image_src_routes_through_asset():
    from silica.ui.web.server import _linkify

    html = _linkify("![alt](img/pic.png) ![ext](https://x.io/p.png)", _fake_resolve)
    assert 'src="/asset?path=img/pic.png"' in html
    assert 'src="https://x.io/p.png"' in html


def test_raw_html_relative_image_src_routes_through_asset():
    # A note written for GitHub uses <img src="..."> rather than ![alt](...), and
    # commonmark passes that through untouched — so the browser resolved it
    # against the page origin and every such image 404'd in the drawer.
    from silica.ui.web.server import _linkify

    html = _linkify('<p align="center"><img src="assets/demo.gif" alt="demo" width="900" /></p>', _fake_resolve)
    assert 'src="/asset?path=assets/demo.gif"' in html

    # inline, single-quoted, and the three forms that must NOT be rewritten
    html = _linkify(
        "text <img src='img/a b.png'> and "
        '<img src="https://x.io/p.png"> and <img src="/asset?path=already.png"> and '
        '<img src="data:image/png;base64,AA">',
        _fake_resolve,
    )
    assert "src='/asset?path=img/a%20b.png'" in html
    assert 'src="https://x.io/p.png"' in html
    assert 'src="/asset?path=already.png"' in html
    assert 'src="data:image/png;base64,AA"' in html


def test_fence_gets_pygments_spans():
    from silica.ui.web.server import _linkify

    html = _linkify('```python\ndef f():\n    return "x"\n```', _fake_resolve)
    assert '<span class="k">def</span>' in html
    assert 'language-python' in html
    # unknown language degrades to a plain escaped fence
    assert "<span" not in _linkify("```nolang\nx\n```", _fake_resolve)


def test_command_output_fence_is_the_class_the_stylesheet_wraps():
    """The ```text fence a slash command's output is wrapped in must land on the
    one class app.css lets wrap. Both halves are needed: the fence renders to
    `language-text`, and that selector carries pre-wrap. Miss either and the
    tail of a message runs off the right edge, which is how /fetch's yt-dlp
    error hid the pip command it prescribes."""
    from pathlib import Path

    from silica.ui.web.server import _linkify

    assert 'class="language-text"' in _linkify("```text\nFetched\n```", _fake_resolve)
    css = (Path(__file__).parent.parent / "silica/ui/web/static/app.css").read_text()
    rule = css.split("pre code.language-text {")[1].split("}")[0]
    assert "white-space: pre-wrap" in rule


def test_asset_endpoint_serves_vault_images_and_closes_traversal(client, tmp_vault):
    from pathlib import Path as _Path

    from silica.config import CONFIG

    tc, _server = client
    tmp_vault.note("img/pic.png", "fake-bytes")
    tmp_vault.note("secret.txt", "no")
    # image that only exists one level above the vault root
    (_Path(CONFIG.vault_path).parent / "outside.png").write_text("leak", encoding="utf-8")

    assert tc.get("/asset", params={"path": "img/pic.png"}).status_code == 200
    # `![[pic.png]]` names the attachment by basename though it lives in img/
    assert tc.get("/asset", params={"path": "pic.png"}).status_code == 200
    assert tc.get("/asset", params={"path": "secret.txt"}).status_code == 404  # not whitelisted
    assert tc.get("/asset", params={"path": "missing.png"}).status_code == 404
    # traversal stays closed: the basename fallback only ever serves an in-vault
    # file, never one living outside the vault, whatever the path spelling.
    assert tc.get("/asset", params={"path": "outside.png"}).status_code == 404
    assert tc.get("/asset", params={"path": "../outside.png"}).status_code == 404
    assert tc.get("/asset", params={"path": "../../outside.png"}).status_code == 404


def test_latex_inline_and_block_become_mathml():
    from silica.ui.web.server import _linkify

    html = _linkify("energy $E=mc^2$ here", _fake_resolve)
    assert "<math" in html and "$" not in html

    html = _linkify("$$\n\\frac{a}{b}\n$$", _fake_resolve)
    assert '<div class="math">' in html
    assert 'display="block"' in html


def test_latex_prose_dollars_and_code_stay_literal():
    from silica.ui.web.server import _linkify

    html = _linkify("costs $5 and $10 today", _fake_resolve)
    assert "<math" not in html
    html = _linkify("run `$x^2$` inline", _fake_resolve)
    assert "<math" not in html and "$x^2$" in html


def test_split_frontmatter_returns_props_and_body():
    from silica.ui.web.server import _split_frontmatter

    props, body = _split_frontmatter("---\ntags: [a, b]\nstatus: seed\n---\n# Title\n")
    assert props == {"tags": ["a", "b"], "status": "seed"}
    assert body == "# Title\n"


def test_split_frontmatter_absent_or_non_mapping_is_none():
    from silica.ui.web.server import _split_frontmatter

    assert _split_frontmatter("# no fm")[0] is None
    assert _split_frontmatter("---\n- just\n- a list\n---\nbody")[0] is None


def test_note_endpoint_renders_frontmatter_properties_box(client, tmp_vault):
    tc, _server = client
    tmp_vault.note("Foo.md", "---\ntags: [x]\nstatus: seed\n---\nbody ==lit==")

    html = tc.get("/note", params={"path": "Foo.md"}).json()["html"]
    assert '<details class="fm"' in html
    assert '<span class="fm-key">tags</span>' in html
    assert '<span class="fm-val">x</span>' in html
    assert "<mark>lit</mark>" in html
    assert "<hr" not in html  # the --- fences never reach the markdown renderer


# ---------------------------------------------------------------------------
# GET /note — read-only rendered note for the drawer.
# ---------------------------------------------------------------------------

def test_note_endpoint_returns_title_and_linkified_html(client, tmp_vault):
    tc, _server = client
    tmp_vault.note("Foo.md", "# Foo")
    tmp_vault.note("concepts/mind-maps.md", "body links to [[Foo]] inside")

    data = tc.get("/note", params={"path": "concepts/mind-maps.md"}).json()
    assert data["title"] == "mind-maps"
    assert 'class="note-link"' in data["html"]
    assert 'data-path="Foo.md"' in data["html"]


def test_note_endpoint_missing_path_is_graceful_not_500(client, tmp_vault):
    tc, _server = client
    r = tc.get("/note", params={"path": "does/not/exist.md"})
    assert r.status_code == 200
    assert "html" in r.json()


def test_note_endpoint_rejects_path_outside_vault(client, tmp_vault):
    tc, _server = client
    r = tc.get("/note", params={"path": "../../etc/passwd"})
    assert r.status_code == 200
    assert "note-link" not in r.json()["html"]  # nothing read, graceful message


# ---------------------------------------------------------------------------
# GET /find — direct semantic-search panel, bypasses the agent.
# ---------------------------------------------------------------------------

def test_find_endpoint_requires_a_query(client):
    tc, _server = client
    r = tc.get("/find", params={"q": ""})
    assert r.status_code == 200
    assert "usage: /find" in r.text


def test_find_endpoint_reports_empty_index_gracefully(client, tmp_path, monkeypatch):
    tc, _server = client
    monkeypatch.setattr("silica.kernel.recall.embed._index_path", lambda: tmp_path / "empty.json")
    r = tc.get("/find", params={"q": "gears"})
    assert r.status_code == 200
    # Both legs empty (embed + co-occurrence) → the facade reports no index.
    assert "No index available" in r.text


def test_find_endpoint_renders_results_as_note_links(client, tmp_path, monkeypatch):
    from unittest.mock import MagicMock, patch
    from silica.kernel.recall.embed import EmbedStore

    tc, _server = client
    idx = tmp_path / "embeddings.json"
    monkeypatch.setattr("silica.kernel.recall.embed._index_path", lambda: idx)
    store = EmbedStore(idx)
    store.upsert("Concepts/A", "A", [1.0, 0.0])
    store.save()

    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [[1.0, 0.0]]
    with patch("silica.agent.providers.get_embedder", return_value=mock_embedder):
        r = tc.get("/find", params={"q": "gears", "k": 1})

    assert r.status_code == 200
    assert 'data-path="Concepts/A"' in r.text
    assert "find-score" in r.text


# ---------------------------------------------------------------------------
# GET /messages — context-token usage rides response headers.
# ---------------------------------------------------------------------------

def test_messages_endpoint_reports_context_token_headers(client, monkeypatch):
    tc, server = client
    from silica.config import CONFIG

    monkeypatch.setattr(CONFIG, "context_tokens", 42)
    monkeypatch.setattr(CONFIG, "max_context_tokens", 1000)
    r = tc.get("/messages")
    assert r.headers["X-Silica-Context-Tokens"] == "42"
    assert r.headers["X-Silica-Max-Context-Tokens"] == "1000"


def test_chat_done_html_linkifies_a_cited_note(client, tmp_vault, monkeypatch):
    tc, server = client
    tmp_vault.note("Foo.md", "# Foo")

    def fake_run_agent(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
        messages.append({"role": "assistant", "content": "look at [[Foo]]"})
        return "look at [[Foo]]"

    monkeypatch.setattr(server, "run_agent", fake_run_agent)
    events = _read_sse(tc.post("/chat", json={"text": "where?"}))
    done = events[-1]
    assert done["type"] == "done"
    assert 'class="note-link"' in done["html"]
    assert 'data-path="Foo.md"' in done["html"]


def test_graph_route_builds_unified_export(client, monkeypatch):
    """GET /graph builds the one unified graph via export_graph (no mode param)."""
    import silica.ui.web.graph_view as gv

    tc, _server = client
    seen = {}

    def spy(output_path, folder="", title="Vault Graph", knn_k=6):
        seen["called"] = True
        Path(output_path).write_text("<html>stub</html>", encoding="utf-8")
        return {"success": True, "path": output_path, "nodes": 0, "edges": 0,
                "similar": 0, "communities": 0, "unresolved": 0, "gaps": 0}

    monkeypatch.setattr(gv, "export_graph", spy)
    assert tc.get("/graph").status_code == 200
    assert seen["called"] is True


def test_top_hubs_ranks_by_resolved_degree():
    """The map landing picker ranks notes by resolved-link degree, skips ghost
    and unlinked nodes, and caps the list."""
    from silica.ui.web.server import _top_hubs

    nodes = [
        {"id": "a", "path": "a.md", "label": "A", "type": "note"},
        {"id": "b", "path": "b.md", "label": "B", "type": "note"},
        {"id": "c", "path": "c.md", "label": "C", "type": "note"},   # unlinked
        {"id": "g", "path": "", "label": "ghost", "type": "ghost"},  # skipped
    ]
    edges = [
        {"from": "a", "to": "b", "type": "EXTRACTED"},
        {"from": "a", "to": "g", "type": "EXTRACTED"},   # a has degree 2
        {"from": "a", "to": "b", "type": "AMBIGUOUS"},   # unresolved: ignored
    ]
    hubs = _top_hubs(nodes, edges, top_n=10)
    assert [h["path"] for h in hubs] == ["a.md", "b.md"]  # a(2) > b(1); c(0) dropped
    assert hubs[0]["degree"] == 2 and hubs[0]["name"] == "A"
    assert _top_hubs(nodes, edges, top_n=1) == hubs[:1]   # cap honored


def test_config_is_the_headers_cheap_read_and_no_longer_writes(client, monkeypatch):
    # /config survives the settings panel as the header label's own read: GET
    # /settings probes four endpoints for their model lists, which the chip that
    # names the active model must not wait seconds for. Its write half moved —
    # `thinking` is a persisted settings row now, not a session-only flip.
    # Empty model skips the network probe in model_limits, so this stays offline.
    from silica.config import CONFIG

    tc, _server = client
    monkeypatch.setattr(CONFIG, "model", "")
    monkeypatch.setattr(CONFIG, "show_thinking", False)

    got = tc.get("/config").json()
    assert set(got) >= {"model", "provider", "context_window", "show_thinking"}
    assert got["show_thinking"] is False

    assert tc.post("/config", json={"show_thinking": True}).status_code == 405
    assert CONFIG.show_thinking is False


# ---------------------------------------------------------------------------
# GET /metrics — the metrics tab's whole payload, one full report pass.
# ---------------------------------------------------------------------------

def test_metrics_endpoint_shapes_the_report_for_the_dashboard(client, tmp_vault):
    # Two linked notes, one orphan, one wikilink into the void — enough to put a
    # value in every structural bucket the view reads.
    tc, _server = client
    tmp_vault.note("A.md", "links to [[B]] and to [[Nowhere]]")
    tmp_vault.note("B.md", "back to [[A]]")
    tmp_vault.note("Lonely.md", "no links at all")

    d = tc.get("/metrics").json()
    assert "error" not in d, d
    assert d["totals"]["notes"] == 3
    # Default is the cheap depth: the co-occurrence leg (~100x the rest) never
    # ran, so `deficits` is absent rather than printed as a measured 0.00.
    assert d["depth"] == "structural"
    assert {t["name"] for t in d["energy"]["terms"]} == {
        "cohesion", "orphans", "dangling", "gaps", "contested",
    }
    # The terms sum to the headline: ΔE between two runs has to decompose.
    assert round(sum(t["value"] for t in d["energy"]["terms"]), 2) == d["energy"]["total"]
    assert "Nowhere" in [x["target"] for x in d["dangling"]]
    assert "Lonely.md" in [o["path"] for o in d["orphans"]]
    # Every note-shaped row carries the path the drawer opens on click.
    for row in d["orphans"] + d["hubs"]:
        assert row["path"]

    full = tc.get("/metrics", params={"proposals": 1}).json()
    assert full["depth"] == "full"
    assert {t["name"] for t in full["energy"]["terms"]} == {
        "cohesion", "orphans", "dangling", "gaps", "deficits", "contested",
    }
    assert round(sum(t["value"] for t in full["energy"]["terms"]), 2) == full["energy"]["total"]


def test_metrics_caps_the_uncapped_lists_without_hiding_the_count(client, tmp_vault, monkeypatch):
    # orphans/dangling are exhaustive in the report; the view gets a slice, and
    # totals keeps the true length so a cut list can't read as the whole list.
    from silica.ui.web import server

    monkeypatch.setattr(server, "_METRICS_ROWS", 2)
    tc, _server = client
    for i in range(5):
        tmp_vault.note(f"O{i}.md", "no links")

    d = tc.get("/metrics").json()
    assert len(d["orphans"]) == 2
    assert d["totals"]["orphans"] == 5


def test_metrics_caps_autolink_candidates(client, tmp_vault, monkeypatch):
    # The co-occurrence leg caps itself at top_k, but the import-derived
    # candidates _compute_code_signals appends are exhaustive — 13k pairs on a
    # 400-note vault, which shipped a 4 MB payload and rendered a card 390,000
    # px tall. Same contract as the lists above: slice the rows, keep the count.
    from silica.kernel.report import graph_report
    from silica.kernel.report.graph_report.models import AutolinkCandidate
    from silica.ui.web import server

    monkeypatch.setattr(server, "_METRICS_ROWS", 2)
    tmp_vault.note("A.md", "solo")
    real = graph_report.compute_report

    def padded(**kw):
        report = real(**kw)
        report.autolink_candidates = [
            AutolinkCandidate(source=f"a{i}.md", target=f"b{i}.md", weight=1.0, shared=["x"])
            for i in range(5)
        ]
        report.totals["autolink_candidates"] = 5
        return report

    monkeypatch.setattr(graph_report, "compute_report", padded)
    tc, _server = client

    d = tc.get("/metrics").json()
    assert len(d["autolinks"]) == 2
    assert d["totals"]["autolink_candidates"] == 5


def test_metrics_gaps_carry_the_sizes_that_rank_them(client, tmp_vault):
    # gap_score = size_a * size_b / (1 + inter_edges), so the two area sizes are
    # what explains the ordering. gap_density is not sent: it reads 99.7-100% on
    # every row of a real vault, and a constant column can't explain an order.
    tc, _server = client
    tmp_vault.note("A.md", "links to [[B]]")
    tmp_vault.note("B.md", "back to [[A]]")
    tmp_vault.note("C.md", "links to [[D]]")
    tmp_vault.note("D.md", "back to [[C]]")

    d = tc.get("/metrics").json()
    assert d["gaps"], "two disconnected pairs must measure as a gap, or this asserts nothing"
    for gap in d["gaps"]:
        assert gap["size_a"] >= 1 and gap["size_b"] >= 1
        assert "density" not in gap


def test_degree_histogram_bins_are_heavy_tail_shaped_and_trim_empty_tail():
    from silica.ui.web.server import _degree_histogram

    # 3 isolated, 2 leaves, 1 note at degree 7 (the 5-8 bucket).
    bins = _degree_histogram({"a": 0, "b": 0, "c": 0, "d": 1, "e": 1, "f": 7})
    assert [(b["label"], b["count"]) for b in bins] == [
        ("0", 3), ("1", 2), ("2", 0), ("3-4", 0), ("5-8", 1),
    ]
    # Interior zeros survive (a hole in the distribution is a reading); the
    # empty tail above the largest degree is dropped.
    assert not any(b["label"].endswith("+") for b in bins)

    # A hub past the last named bucket lands in the open-ended one.
    top = _degree_histogram({"h": 400})[-1]
    assert top["label"] == "65+" and top["count"] == 1

    # Never returns an empty axis, even for an empty vault.
    assert len(_degree_histogram({})) == 1


def test_metrics_reports_a_degree_distribution_over_every_note(client, tmp_vault):
    tc, _server = client
    tmp_vault.note("Hub.md", "[[A]] [[B]]")
    tmp_vault.note("A.md", "[[Hub]]")
    tmp_vault.note("B.md", "[[Hub]]")
    tmp_vault.note("Alone.md", "no links")

    d = tc.get("/metrics").json()
    hist = d["degree_histogram"]
    # Every note is binned exactly once — a distribution that drops notes lies.
    assert sum(b["count"] for b in hist) == d["totals"]["notes"] == 4
    assert hist[0]["label"] == "0" and hist[0]["count"] == 1  # Alone


def test_degree_map_is_populated_without_analytics(tmp_vault):
    # degree falls out of the structural core, so the cheap nucleate path that
    # skips PageRank/betweenness still carries it.
    from silica.kernel.report.graph_report import compute_report

    tmp_vault.note("A.md", "[[B]]")
    tmp_vault.note("B.md", "[[A]]")

    cheap = compute_report()
    # Same keyspace as its sibling maps — a degree map keyed differently from
    # pagerank_map could not be joined against them.
    assert cheap.degree_map and set(cheap.degree_map) == set(cheap.pagerank_map)
    assert set(cheap.degree_map.values()) == {2}  # A<->B, one link each way
    # …while betweenness, the analytics-only sibling, is still zero-filled here.
    assert not any(cheap.betweenness_map.values())
    assert compute_report(analytics=True).degree_map == cheap.degree_map


class TestOwnSessionCapture:
    """The GUI flushes its conversation where the server can see the end of it."""

    @pytest.fixture(autouse=True)
    def _opt_in(self, client, tmp_path, monkeypatch):
        import silica.kernel.recall.paths as paths
        from silica.config import CONFIG

        monkeypatch.setattr(paths, "_SILICA_HOME", tmp_path / "silica-home")
        monkeypatch.setattr(CONFIG, "capture_sessions", True)
        _, server = client
        server.messages.extend([
            {"role": "user", "content": "does the GUI capture its own chats?"},
            {"role": "assistant", "content": "Only when you opt in. " * 20},
        ])

    def _envelopes(self):
        from silica.config import CONFIG
        from silica.kernel.recall.paths import inbox_dir_for
        d = inbox_dir_for(CONFIG.vault_path)
        return sorted(p.name for p in d.glob("*.json")) if d.is_dir() else []

    def test_a_new_chat_flushes_the_one_being_replaced(self, client):
        tc, _ = client

        assert tc.post("/reset").status_code == 200

        assert len(self._envelopes()) == 1

    def test_shutting_the_server_down_flushes_the_live_chat(self, client, monkeypatch):
        _, server = client
        monkeypatch.setattr(server, "_reset_session", lambda: None)  # keep the history
        import uvicorn
        monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)
        monkeypatch.setattr(server, "print_banner", lambda: None, raising=False)

        server.serve(port=0)

        assert len(self._envelopes()) == 1


# --- dictation ---------------------------------------------------------------
# The browser records and converts; the server's whole job is to know whether an
# endpoint is there and to forward one WAV to it. Both halves are guarded here
# because the failure mode is silent: an unreachable endpoint that answers 200
# would put an empty string in the composer and look like a bad microphone.


def test_stt_status_reports_an_unconfigured_endpoint(client, monkeypatch):
    app, server = client
    monkeypatch.setattr(server.CONFIG, "stt_base_url", "", raising=False)
    body = app.get("/stt").json()
    assert body["ok"] is False
    assert "SILICA_STT_BASE_URL" in body["detail"]


def test_stt_status_probes_the_endpoint_rather_than_trusting_the_setting(client, monkeypatch):
    app, server = client
    monkeypatch.setattr(server.CONFIG, "stt_base_url", "http://localhost:9/v1", raising=False)
    from silica.onboarding import serve

    monkeypatch.setattr(serve, "ready", lambda url: False)
    body = app.get("/stt").json()
    assert body["ok"] is False
    assert "localhost:9" in body["detail"]

    monkeypatch.setattr(serve, "ready", lambda url: True)
    assert app.get("/stt").json()["ok"] is True


def test_stt_forwards_the_clip_and_returns_the_text(client, monkeypatch):
    app, server = client
    monkeypatch.setattr(server.CONFIG, "stt_base_url", "http://localhost:9/v1", raising=False)
    monkeypatch.setattr(server.CONFIG, "stt_lang", "it", raising=False)
    seen = {}

    class _Resp:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"text": "  ciao mondo  "}

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, files, data, headers):
            seen["url"] = url
            seen["lang"] = data.get("language")
            seen["bytes"] = files["file"][1]
            return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    resp = app.post("/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})
    assert resp.json() == {"text": "ciao mondo"}
    assert seen["url"] == "http://localhost:9/v1/audio/transcriptions"
    # whisper-server assumes English without this, so a silently dropped language
    # would come back as a translation of an Italian vault
    assert seen["lang"] == "it"
    assert seen["bytes"] == b"RIFFfake"


def test_stt_rejects_an_empty_recording(client, monkeypatch):
    app, server = client
    monkeypatch.setattr(server.CONFIG, "stt_base_url", "http://localhost:9/v1", raising=False)
    assert app.post("/stt", files={"audio": ("clip.wav", b"", "audio/wav")}).status_code == 400


# --- theme -------------------------------------------------------------------
# The palette is CSS and needs no test. What does is the seam that decides WHICH
# palette: a preference the server stamps, a resolution only the browser can do,
# and three documents that have to agree across two iframe boundaries.

def test_index_stamps_the_configured_theme_preference(client, monkeypatch):
    """The server ships the preference, never the resolution.

    A theme resolved server-side cannot answer "auto", and one resolved only in
    JS after load flashes the wrong palette for a frame. So `/` carries
    SILICA_THEME through to the attribute the <head> script reads.
    """
    app, server = client
    for pref in ("light", "dark", "auto"):
        monkeypatch.setattr(server.CONFIG, "theme", pref, raising=False)
        assert f'data-theme-pref="{pref}"' in app.get("/").text

    # An unknown value degrades to auto rather than stamping something the CSS
    # has no block for, which would silently pin the app to dark.
    monkeypatch.setattr(server.CONFIG, "theme", "sepia", raising=False)
    assert 'data-theme-pref="auto"' in app.get("/").text


def test_theme_is_an_admitted_setting(tmp_path, monkeypatch):
    """Reachable from the panel, and only as the three values CSS implements.

    resolve_env_path is redirected because apply() persists: without it this
    test writes SILICA_THEME into whichever .env the developer is actually
    using, and pins their app to whatever the assertion happened to pass.
    """
    from silica.ui.web import settings as st

    monkeypatch.setattr(
        "silica.onboarding.wizard.resolve_env_path", lambda: tmp_path / ".env")
    monkeypatch.delenv("SILICA_THEME", raising=False)
    monkeypatch.setattr(st, "SHELL_ENV", {})

    row = next(r for r in st.sections()["Display"] if r.key == "SILICA_THEME")
    assert row.kind == "enum" and row.options == ("auto", "dark", "light")
    assert st.apply("SILICA_THEME", "light")["ok"] is True
    assert "SILICA_THEME=light" in (tmp_path / ".env").read_text()


def test_the_graph_document_carries_both_palettes(tmp_path, monkeypatch):
    """/graph is a standalone document with no stylesheet to link to.

    It ships both ramps and resolves between them itself — ?theme= when the app
    embeds it, the OS when it is opened off disk — so a light session cannot end
    at the iframe boundary.
    """
    import silica.kernel.recall.graph_export as ge
    from silica.ui.web import graph_view as gv

    monkeypatch.setattr(ge, "build_graph_data", lambda folder="": (
        [{"id": "a.md", "label": "a", "type": "note"}], []))
    monkeypatch.setattr(ge, "detect_communities", lambda nodes, edges: [])
    monkeypatch.setattr(ge, "knn_edges", lambda nodes, k=6: [])

    out = tmp_path / "g.html"
    gv.export_graph(output_path=str(out))
    html = out.read_text(encoding="utf-8")

    assert ':root[data-theme="light"]' in html      # the second ramp
    assert 'get("theme")' in html                   # the app pins it
    assert "prefers-color-scheme: light" in html    # file:// falls back to the OS
    # The canvas cannot read a CSS token, so it carries its own pair.
    assert "const LIGHT =" in html and "const GP =" in html


def test_community_colour_has_a_paper_band():
    """Same hue and the same golden-ratio walk, a different lightness band.

    The dark band is picked to survive a near-black floor; reusing it on paper
    puts every community dot at roughly the contrast of the page it sits on.
    """
    from silica.kernel.recall.graph_export import _community_color, _zone_color

    for i in range(6):
        assert _community_color(i) != _community_color(i, on_paper=True)
        assert _zone_color(i) != _zone_color(i, on_paper=True)
    # and the two partitions still do not share a colour key (ADR-0023)
    assert _community_color(3, on_paper=True) != _zone_color(3, on_paper=True)


def test_health_hides_the_cli_hook_check_from_the_sidebar(client, monkeypatch):
    """"session capture" tells a GUI user to edit .claude/settings.json — a
    Claude-Code-integration concern the browser can do nothing about. It
    stays out of the sidebar notices and stays IN the ?all=1 diagnostics."""
    tc, _ = client
    import silica.onboarding.checks as checks

    monkeypatch.setattr(checks, "run_checks", lambda cfg: [
        checks.CheckResult("session capture", "warn", "no hook", "edit settings.json"),
        checks.CheckResult("embeddings", "warn", "down", "start it"),
    ])
    assert [r["name"] for r in tc.get("/health").json()] == ["embeddings"]
    assert [r["name"] for r in tc.get("/health?all=1").json()] == [
        "session capture", "embeddings",
    ]


# --- calendar ----------------------------------------------------------------

EVENT_NOTE_FM = "---\ntitle: Dentist\nevent_start: 2026-08-20 15:00\n---\n"


def test_calendar_endpoint_serves_the_agenda_days(client, tmp_vault):
    tc, _server = client
    tmp_vault.note("calendar/2026-08-20 Dentist.md", EVENT_NOTE_FM)

    d = tc.get("/calendar", params={"start": "2026-08-17", "days": 7}).json()
    assert "error" not in d, d
    assert d["start"] == "2026-08-17" and len(d["days"]) == 7
    day = {r["date"]: r for r in d["days"]}["2026-08-20"]
    assert [e["title"] for e in day["events"]] == ["Dentist"]


def test_calendar_endpoint_rejects_a_bad_start(client):
    tc, _server = client
    assert "error" in tc.get("/calendar", params={"start": "not-a-date"}).json()


def test_reminders_poll_is_post_and_delivers_at_most_once(client, tmp_vault):
    # One-shot in the past with an at-start reminder: exactly one late notice
    # on the first poll, none on the second (the mark advanced), and GET is
    # not an allowed method (the poll advances marks — a mutation).
    tc, _server = client
    tmp_vault.note("calendar/call.md",
                   "---\nevent_start: 2026-08-10 15:00\nevent_reminder: 0m\n---\n")

    first = tc.post("/reminders").json()
    assert [(r["title"], r["late"]) for r in first["due"]] == [("call", True)]
    assert tc.post("/reminders").json() == {"due": []}
    assert tc.get("/reminders").status_code == 405


def test_gui_seed_is_the_tui_seed(tmp_vault, monkeypatch):
    """The GUI used to build its own seed (prompt + vault map) and had drifted:
    no `_vault_scope`, so the model was blind to `write_dir`, and no closing
    language line — the fix f104232 shipped for the TUI and never carried over.
    Both now come from `cli.seed_messages`, so the drift cannot come back."""
    import datetime as dt

    from silica.cli import seed_messages
    from silica.ui.web import server

    server._build_seed()
    gui = [m["content"] for m in server._seed[0]]
    tui = [m["content"] for m in seed_messages()]

    assert len(gui) == len(tui)
    # Same messages, one difference: the GUI prompt carries the math block.
    assert gui[1:] == tui[1:]
    assert "$$" in gui[0] and "$$" not in tui[0]
    # And the two things the GUI was missing, by content rather than by parity:
    # what day it is (nothing else in the seed carries a date) and the read/write
    # scope. The closing line is the restated language rule.
    assert dt.date.today().isoformat() in gui[1]
    assert gui[2].startswith("Vault: ")
    assert "language" in gui[-1].lower()
def test_vault_brief_is_off_when_the_setting_is_off(client, monkeypatch):
    """The written sentence is the switchable half of the landing. Off, the
    endpoint answers without touching a provider — the counted line the browser
    renders beside it is what keeps the landing from going blank."""
    from silica.config import CONFIG
    from silica.ui.web import server

    monkeypatch.setattr(CONFIG, "vault_brief", False)
    monkeypatch.setattr(server, "_write_brief",
                        lambda *a: pytest.fail("a disabled brief called the model"))
    assert server.vault_brief() == {"enabled": False, "text": ""}


def test_vault_brief_replays_its_cache_until_the_corpus_shape_changes(
        client, monkeypatch, tmp_path):
    """The sentence is written against a corpus, so the stamp is the corpus: a
    vault that only sat there replays, a vault that grew is written again. A
    brief that regenerated per request would put a model call on every reload
    of the chat tab.

    The graph is stubbed rather than grown on disk: this is a check on the
    stamp, and building it out of real notes would make it a check on when the
    index picks a new file up instead."""
    from silica.config import CONFIG
    from silica.kernel.recall import graph_export
    from silica.ui.web import server

    monkeypatch.setattr(CONFIG, "vault_brief", True)
    monkeypatch.setattr(server, "_brief_path", lambda: tmp_path / "brief.json")

    nodes = [{"id": "a", "path": "a.md", "label": "a"},
             {"id": "b", "path": "b.md", "label": "b"}]
    monkeypatch.setattr(graph_export, "build_graph_data", lambda **_k: (nodes, []))
    monkeypatch.setattr(graph_export, "detect_communities", lambda *_a: [])

    calls = []
    monkeypatch.setattr(server, "_write_brief",
                        lambda n, t, h: calls.append(n) or f"about {n} notes")

    assert server.vault_brief()["text"] == "about 2 notes"
    assert server.vault_brief() == {"enabled": True, "text": "about 2 notes",
                                    "cached": True}
    assert calls == [2], "the second call should have replayed the cache"

    nodes.append({"id": "c", "path": "c.md", "label": "c"})
    assert server.vault_brief()["text"] == "about 3 notes"
    assert calls == [2, 3]


def test_vault_brief_survives_a_provider_that_is_not_there(
        client, monkeypatch, tmp_path):
    """A landing that 500s because a worker model is down would be worse than a
    landing with one fewer line on it."""
    from silica.config import CONFIG
    from silica.ui.web import server

    monkeypatch.setattr(CONFIG, "vault_brief", True)
    monkeypatch.setattr(server, "_brief_path", lambda: tmp_path / "brief.json")

    def _boom(*_a, **_k):
        raise ConnectionError("no worker here")

    monkeypatch.setattr("silica.agent.providers.get_provider", _boom)
    assert server.vault_brief() == {"enabled": True, "text": ""}
    assert not (tmp_path / "brief.json").exists(), "an empty brief was cached"


def test_graph_document_revalidates_and_compresses(client):
    """/graph inlines both force-graph bundles, so it is megabytes per visit. It
    used to ship with no validator and no encoding, refetched in full every time
    the explore tab was opened."""
    tc, _server = client

    r = tc.get("/graph", headers={"accept-encoding": "gzip"})
    assert r.status_code == 200
    etag = r.headers.get("etag")
    assert etag, "no ETag: every visit refetches the whole document"
    assert r.headers.get("cache-control") == "no-cache"

    # httpx transparently decodes, so check the wire header rather than the body
    assert r.headers.get("content-encoding") == "gzip"
    assert r.headers.get("vary") == "Accept-Encoding"

    # a revisit costs a 304 and no body
    again = tc.get("/graph", headers={"if-none-match": etag})
    assert again.status_code == 304
    assert not again.content

    # and the identity path still serves a real document
    plain = tc.get("/graph", headers={"accept-encoding": "identity"})
    assert plain.status_code == 200
    assert "content-encoding" not in plain.headers
    assert plain.text.lstrip().lower().startswith("<!doctype html")


def test_reduced_motion_is_honoured_for_transitions_not_only_animations():
    """The file carries dozens of transitions and only four were switched off,
    so a session that asked the OS for less motion still got the header, the
    tabs, the sidebar and every row moving."""
    from silica.ui.web.server import STATIC_DIR

    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    blocks = css.split("@media (prefers-reduced-motion: reduce)")
    assert len(blocks) > 1, "no reduced-motion handling at all"
    blanket = [b for b in blocks[1:] if b.lstrip().startswith("{\n\n  *,")]
    assert blanket, "no blanket rule: coverage is per-selector and will rot"
    assert "transition-duration: 0.01ms !important" in blanket[0]
    assert "animation-duration: 0.01ms !important" in blanket[0]


def test_the_injector_tool_keeps_sources_like_nucleate_does():
    """`/nucleate` defaults keep_sources on — the leaf in sources/ is what makes
    a note's verbatim source reachable at all, and reliability_tier reads exactly
    that. The flag was set only on the CLI side while Coordinator defaults it
    False, so the same file nucleated through the tool (web drag-drop, MCP)
    produced notes whose source could never be checked."""
    import inspect

    from silica.tools.runners import RunInjectorArgs, silica_run_injector

    assert RunInjectorArgs.model_fields["keep_sources"].default is True
    fn = getattr(silica_run_injector, "__wrapped__", silica_run_injector)
    assert inspect.signature(fn).parameters["keep_sources"].default is True
