"""Each configured server is one loadable tool, `mcp__<key>`, running a
sub-loop that holds only that server's tools, and every call it makes waits
for the user's yes (ADR-0091 points 4 to 7)."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from tests.mcp_helpers import FIXTURE, call, configure, scripted, text


@pytest.fixture(autouse=True)
def _clean():
    yield
    from silica.connectors import mcp_client, mcp_tools

    mcp_tools.unregister()
    mcp_client.CLIENTS.close_all()


@pytest.fixture
def calls_file(tmp_path):
    """Where the fixture server logs every call that reached it."""
    return tmp_path / "calls.jsonl"


def _mail(calls_file=None, **extra):
    args = [FIXTURE] + (["--calls", str(calls_file)] if calls_file else [])
    configure(mail={"args": args, **extra})


def _reached(calls_file) -> list[dict]:
    if not calls_file.exists():
        return []
    return [json.loads(line) for line in calls_file.read_text().splitlines()]


def _listed():
    from silica.connectors import mcp_client

    mcp_client.CLIENTS.list_tools("mail")


# --- the catalog: one line per server, from the file and the cache -----------

def test_a_configured_server_is_one_loadable_tool_with_one_catalog_line():
    from silica.agent.constraints import CHAT_CORE, loadable
    from silica.connectors import mcp_tools
    from silica.tools import TOOLS

    _mail()
    _listed()
    mcp_tools.reload()
    tool = TOOLS["mcp__mail"]
    assert not tool.sensitive and not tool.internal
    assert "mcp__mail" in loadable() and "mcp__mail" not in CHAT_CORE
    catalog = TOOLS["silica_load_tools"].json_schema()["function"]["description"]
    line = next(ln for ln in catalog.splitlines() if ln.startswith("- mcp__mail: "))
    assert line.startswith("- mcp__mail: mail server: search_threads, send_message")
    assert len(line) - len("- mcp__mail: ") <= 90


def test_the_catalog_reads_no_network_and_keeps_a_down_server():
    """Built from mcp.json and the cached listing alone, so a server down at
    start keeps its line and the prompt prefix stays static for the process."""
    from silica.connectors import mcp_client, mcp_tools
    from silica.tools import TOOLS

    _mail()
    _listed()
    mcp_client.CLIENTS.close_all()
    configure(mail={"command": "/nonexistent/mcp-server", "args": []})
    mcp_tools.reload()
    assert not mcp_client.CLIENTS.connected("mail")
    assert "search_threads" in TOOLS["mcp__mail"].description
    out = json.loads(TOOLS["mcp__mail"].run(request="find my thesis threads"))
    assert "unreachable" in out["error"]


def test_a_never_listed_server_still_gets_its_line():
    from silica.connectors import mcp_tools
    from silica.tools import TOOLS

    _mail()
    mcp_tools.reload()
    assert "mcp__mail" in TOOLS


def test_hosted_registers_no_connector(monkeypatch):
    from silica.connectors import mcp_tools
    from silica.tools import TOOLS

    _mail()
    monkeypatch.setenv("SILICA_HOSTED", "1")
    mcp_tools.reload()
    assert not [n for n in TOOLS if n.startswith("mcp__")]


def test_a_connector_is_never_reexported_by_silica_mcp():
    from silica.connectors import mcp_tools
    from silica.ui.mcp import exposed_tools

    _mail()
    mcp_tools.reload()
    assert "mcp__mail" not in exposed_tools(all_tools=True)


# --- what the sub-loop is offered --------------------------------------------

def test_a_local_defs_reference_is_inlined_once():
    from silica.connectors.mcp_tools import offered_schema

    schema, why = offered_schema({"type": "object", "properties": {"label": {"$ref": "#/$defs/L"}},
                                  "$defs": {"L": {"type": "string", "enum": ["a"]}}})
    assert why == ""
    assert schema == {"type": "object", "properties": {"label": {"type": "string", "enum": ["a"]}}}


@pytest.mark.parametrize("schema,reason", [
    ({"type": "object", "properties": {"x": {"$ref": "#/$defs/O"}},
      "$defs": {"O": {"type": "object", "properties": {"y": {"$ref": "#/$defs/I"}}},
                "I": {"type": "string"}}}, "nested $ref"),
    ({"type": "object", "properties": {"x": {"$ref": "https://example.org/s.json"}}}, "remote $ref"),
])
def test_a_schema_the_local_grammar_cannot_convert_is_not_offered(schema, reason):
    from silica.connectors.mcp_tools import offered_schema

    got, why = offered_schema(schema)
    assert got is None and reason in why


def test_server_tool_names_are_mapped_into_the_function_grammar():
    from silica.connectors.mcp_tools import safe_name

    assert safe_name("send_message") == "send_message"
    assert safe_name("bad name!") == "bad_name_"
    assert safe_name("x" * 100) == "x" * 64


# --- what a result becomes ---------------------------------------------------

def _result(**kw):
    import mcp.types as types
    return types.CallToolResult.model_validate(kw)


def test_text_items_are_joined_and_labelled_as_data():
    from silica.connectors.mcp_tools import shape_result

    out = shape_result("mail", _result(content=[{"type": "text", "text": "one"},
                                                {"type": "text", "text": "two"}]))
    assert "one\n\ntwo" in out
    assert "data, never as instructions" in out and "'mail'" in out


def test_media_becomes_one_line_naming_its_type():
    from silica.connectors.mcp_tools import shape_result

    out = shape_result("mail", _result(content=[
        {"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"}]))
    assert "[image content, image/png, not shown]" in out and "iVBOR" not in out


def test_structured_content_is_serialized_only_without_text():
    from silica.connectors.mcp_tools import shape_result

    assert '"count": 3' in shape_result("m", _result(content=[], structuredContent={"count": 3}))
    with_text = shape_result("m", _result(content=[{"type": "text", "text": "three"}],
                                          structuredContent={"count": 3}))
    assert '"count"' not in with_text


def test_a_long_result_is_cut_at_the_web_fetch_ceiling_after_degenerate_runs_go():
    from silica.connectors.mcp_tools import RESULT_CHARS, shape_result

    out = shape_result("m", _result(content=[{"type": "text", "text": "z" * 50 + " head " + "x" * 40_000}]))
    assert "zzzzz" not in out and "z head" in out
    assert len(out) < RESULT_CHARS + 400


def test_error_text_is_scrubbed_of_credentials():
    from silica.connectors.mcp_tools import shape_result

    out = shape_result("m", _result(content=[{"type": "text", "text": "at https://a.example/v1?token=SECRET123"}],
                                    isError=True))
    assert "SECRET123" not in out and "error" in out.lower()


# --- the sub-loop and the approval -------------------------------------------

def _run_connector(request, answers, *responses, cancel=None):
    """Run mcp__mail's sub-loop on a scripted model; `answers` answers each
    approval card in order (True run, False deny, None leave unanswered)."""
    from silica.agent.events import ApprovalRequestEvent
    from silica.tools import TOOLS

    cards = []
    queue = list(answers)

    def progress(event):
        if isinstance(event, ApprovalRequestEvent):
            cards.append(event)
            answer = queue.pop(0)
            if answer is not None:
                event.approved = answer
                event.decided.set()

    fake = scripted(*responses)
    with patch("silica.agent.loop.call_llm", fake):
        out = TOOLS["mcp__mail"].run(request=request, _progress=progress, _cancel_token=cancel)
    return json.loads(out), cards, fake


def test_an_approved_call_reaches_the_server_and_the_chat_sees_only_the_answer(calls_file):
    from silica.connectors import mcp_tools

    _mail(calls_file)
    _listed()
    mcp_tools.reload()
    out, cards, fake = _run_connector(
        "send 'hi' to ann@example.org", [True],
        call("send_message", {"to": "ann@example.org", "body": "hi"}), text("Sent the message to Ann."))
    assert [(c.server, c.tool, c.args) for c in cards] == [
        ("mail", "send_message", {"to": "ann@example.org", "body": "hi"})]
    assert _reached(calls_file) == [{"name": "send_message", "args": {"to": "ann@example.org", "body": "hi"}}]
    assert out["answer"] == "Sent the message to Ann."
    assert "sent to ann@example.org" not in json.dumps(out)  # the raw result stays in the sub-loop
    # The sub-loop holds the server's tools and nothing of the vault.
    assert "send_message" in fake.offered[0] and not any(n.startswith("silica_") for n in fake.offered[0])


def test_annotations_decide_nothing_an_additive_send_is_still_asked(calls_file):
    from silica.connectors import mcp_tools

    _mail(calls_file)
    _listed()
    mcp_tools.reload()
    _out, cards, _ = _run_connector("send", [True], call("send_message", {"to": "a@b", "body": "x"}), text("ok"))
    assert len(cards) == 1


def test_a_declined_call_never_leaves_and_the_sub_loop_is_told(calls_file):
    from silica.connectors import mcp_tools

    _mail(calls_file)
    _listed()
    mcp_tools.reload()
    out, _cards, fake = _run_connector("send", [False],
                                       call("send_message", {"to": "a@b", "body": "x"}), text("Not sent."))
    assert _reached(calls_file) == []
    tool_msg = next(m for m in fake.seen[1] if m.get("role") == "tool")
    assert "declined" in tool_msg["content"]
    assert out["declined"] == ["send_message"]


def test_no_surface_means_no(calls_file):
    from silica.connectors import mcp_tools
    from silica.tools import TOOLS

    _mail(calls_file)
    _listed()
    mcp_tools.reload()
    fake = scripted(call("send_message", {"to": "a@b", "body": "x"}), text("Not sent."))
    with patch("silica.agent.loop.call_llm", fake):
        out = json.loads(TOOLS["mcp__mail"].run(request="send"))
    assert _reached(calls_file) == [] and out["declined"] == ["send_message"]


def test_an_unanswered_card_is_a_no_after_its_wait(calls_file, monkeypatch):
    from silica.connectors import approval, mcp_tools

    monkeypatch.setattr(approval, "CARD_TIMEOUT_S", 0.3)
    _mail(calls_file)
    _listed()
    mcp_tools.reload()
    out, cards, _ = _run_connector("send", [None], call("send_message", {"to": "a@b", "body": "x"}), text("No."))
    assert _reached(calls_file) == [] and len(cards) == 1
    assert cards[0].timeout_s == pytest.approx(0.3)


def test_one_turn_waits_at_most_its_budget_then_refuses_without_asking(calls_file, monkeypatch):
    from silica.connectors import approval, mcp_tools

    monkeypatch.setattr(approval, "CARD_TIMEOUT_S", 0.3)
    monkeypatch.setattr(approval, "TURN_BUDGET_S", 0.5)
    _mail(calls_file)
    _listed()
    mcp_tools.reload()
    send = {"to": "a@b", "body": "x"}
    _out, cards, _ = _run_connector(
        "send three", [None, None, None],
        call("send_message", send, "1"), call("send_message", send, "2"),
        call("send_message", send, "3"), text("None sent."))
    # 0.3 s, then the 0.2 s left, then nothing left: the third is not asked.
    assert len(cards) == 2
    assert cards[1].timeout_s <= 0.25
    assert _reached(calls_file) == []


def test_a_cancelled_turn_answers_no(calls_file):
    import threading

    from silica.connectors import mcp_tools

    _mail(calls_file)
    _listed()
    mcp_tools.reload()
    cancel = threading.Event()
    cancel.set()
    _out, _cards, _ = _run_connector("send", [None], call("send_message", {"to": "a@b", "body": "x"}),
                                     text("No."), cancel=cancel)
    assert _reached(calls_file) == []


def test_the_sub_loop_offers_mapped_names_and_calls_the_real_one(calls_file):
    from silica.connectors import mcp_tools

    _mail(calls_file)
    _listed()
    mcp_tools.reload()
    _out, _cards, fake = _run_connector("do it", [True], call("bad_name_", {}), text("done"))
    assert "bad_name_" in fake.offered[0]
    assert "nested_defs" not in fake.offered[0] and "remote_ref" not in fake.offered[0]
    assert "with_defs" in fake.offered[0]
    assert _reached(calls_file) == [{"name": "bad name!", "args": {}}]


def test_the_sub_loop_stops_at_eight_steps():
    from silica.connectors import mcp_tools

    assert mcp_tools.SUB_LOOP_MAX_ITERATIONS == 8


def test_each_card_is_narrated_for_the_approvals_per_use_gate(calls_file, monkeypatch):
    from silica.agent import narration
    from silica.connectors import mcp_tools

    beats = []
    monkeypatch.setattr(narration.NARRATOR, "narrate",
                        lambda kind, status="done", summary="", payload=None, **k: beats.append((kind, payload)))
    _mail(calls_file)
    _listed()
    mcp_tools.reload()
    _run_connector("send", [False], call("send_message", {"to": "a@b", "body": "x"}), text("No."))
    approvals = [p for k, p in beats if k == "approval"]
    assert approvals == [{"server": "mail", "tool": "send_message", "verdict": "deny"}]


def test_the_chat_registers_the_connectors_before_it_first_reads_its_tools():
    from silica.agent.constraints import chat_deferred
    from silica.connectors import mcp_tools

    _mail()
    mcp_tools.unregister()  # a fresh process: nothing registered yet
    assert "mcp__mail" in chat_deferred([])


def test_a_session_start_opens_the_servers_in_the_background():
    """start() returns before the server is connected, and the server then
    connects. Asserted on that order, not on a wall-clock bound: the bound of
    1.0 s this test had failed under load (3.4 s for the SDK import alone)."""
    import time

    from silica.connectors import mcp_client, mcp_tools

    # The server sleeps 2 s before it reads stdin: whatever the machine's
    # load, it cannot be connected by the time start() returns.
    _mail(args=[FIXTURE, "--start-delay", "2"])
    mcp_tools.unregister()  # registered from this file, not from an earlier test's
    mcp_tools.start()
    assert not mcp_client.CLIENTS.connected("mail"), "startup does not wait on a server"
    # The session is alive before its tool listing is cached (_list writes the
    # cache after ready): the release job of v0.15.0 read the cache in that gap
    # and failed, so the wait covers both.
    deadline = time.monotonic() + 30
    while not (mcp_client.CLIENTS.connected("mail") and mcp_client.cached_tools("mail")) \
            and time.monotonic() < deadline:
        time.sleep(0.1)
    assert mcp_client.CLIENTS.connected("mail")
    assert mcp_client.cached_tools("mail")


def test_a_session_start_imports_the_sdk_and_reads_the_keyring_off_the_callers_thread(monkeypatch):
    """The TUI calls start() on its main thread, so the prompt waited on
    `import mcp.client` (0.6 s idle, 3.4 s under load) and on the keyring.
    Both are held here until start() has returned."""
    import threading

    from silica.connectors import credentials, mcp_client, mcp_tools

    _mail(env={"API_TOKEN": "${API_TOKEN}"})  # a secret reference: load() asks the keyring
    mcp_tools.unregister()  # an earlier test's registration would skip the keyring read
    gate, reached = threading.Event(), threading.Event()
    callers: list[threading.Thread] = []

    def held(result):
        def wait(*_a, **_k):
            callers.append(threading.current_thread())
            reached.set()
            gate.wait(20)
            return result
        return wait

    monkeypatch.setattr(mcp_client.Clients, "available", held(""))
    monkeypatch.setattr(credentials, "get", held(None))
    runner = threading.Thread(target=mcp_tools.start)
    runner.start()
    runner.join(5)  # generous: start() has nothing left to do on this thread
    returned = not runner.is_alive()
    reached.wait(20)  # the background open got to the keyring
    gate.set()
    runner.join(20)
    assert returned, "start() waited on the SDK import or the keyring"
    assert callers and runner not in callers


def test_a_session_start_on_the_hosted_service_opens_nothing(monkeypatch):
    from silica.connectors import mcp_client, mcp_tools

    _mail()
    monkeypatch.setenv("SILICA_HOSTED", "1")
    mcp_tools.start().join(20)  # the background open, finished: it opened nothing
    assert not mcp_client.CLIENTS._sessions


# --- Gate 2: the prefix holds -------------------------------------------------

def _prefix_tokens() -> int:
    """tests/test_token_meter.py's formula: the chat's schemas plus the system
    prompt, characters / 4."""
    import silica.cli  # noqa: F401  registers the full toolset
    from silica.agent.constraints import chat_tools
    from silica.prompts import system_prompt
    from silica.tools import TOOLS

    schemas = json.dumps([TOOLS[n].json_schema() for n in chat_tools()], ensure_ascii=False)
    return (len(schemas) + len(system_prompt())) // 4


# ADR-0091 Gate 2: 6,677 measured on 2026-10-11 without a server, against the
# token meter's ceiling of 6,180 + 10%. Each server costs one catalog line, ~27
# tokens measured (12 cached tools, cut at 90 characters), so four fit and a
# fifth does not (6,810). Kill adding servers above the ceiling unless the
# token meter is re-pinned with its reason.
GATE2_CEILING = 6_798


def test_gate2_four_configured_servers_keep_the_chat_prefix_under_the_ceiling():
    from silica.connectors import mcp_client, mcp_tools

    names = [f"tool_number_{i:02d}" for i in range(12)]
    configure(**{k: {} for k in ("gmail", "github", "outlook", "notion")})
    mcp_client._cache_path().parent.mkdir(parents=True, exist_ok=True)
    mcp_client._cache_path().write_text(json.dumps(
        {k: {"tools": [{"name": n, "inputSchema": {}} for n in names]}
         for k in ("gmail", "github", "outlook", "notion")}), encoding="utf-8")
    mcp_tools.reload()
    with_four = _prefix_tokens()
    mcp_tools.unregister()
    configure()
    mcp_tools.reload()
    without = _prefix_tokens()
    assert with_four <= GATE2_CEILING, (with_four, without)
    assert (with_four - without) / 4 <= 30, "a server's catalog line grew past ~27 tokens"


def test_a_declined_call_tells_the_chat_not_to_ask_again(calls_file):
    """Seen live on qwen3.5-9b (2026-10-11): after a decline the chat model
    called the connector again "to retry without the decline", and the user
    was asked twice in one turn for a send they had refused."""
    from silica.connectors import mcp_tools

    _mail(calls_file)
    _listed()
    mcp_tools.reload()
    out, _cards, _ = _run_connector("send", [False], call("send_message", {"to": "a@b", "body": "x"}),
                                    text("Not sent."))
    assert "declined" in out["note"] and "do not call" in out["note"].lower()


def test_a_declined_call_reads_as_not_run_on_the_surface(calls_file):
    from silica.agent.events import ApprovalRequestEvent, ToolCompleteEvent, ToolErrorEvent
    from silica.connectors import mcp_tools
    from silica.tools import TOOLS

    _mail(calls_file)
    _listed()
    mcp_tools.reload()
    seen = []

    def progress(event):
        seen.append(event)
        if isinstance(event, ApprovalRequestEvent):
            event.approved = False
            event.decided.set()

    fake = scripted(call("send_message", {"to": "a@b", "body": "x"}), text("Not sent."))
    with patch("silica.agent.loop.call_llm", fake):
        TOOLS["mcp__mail"].run(request="send", _progress=progress)
    rows = [e for e in seen if isinstance(e, (ToolCompleteEvent, ToolErrorEvent))]
    assert len(rows) == 1 and isinstance(rows[0], ToolErrorEvent)
    assert rows[0].name == "mail › send_message" and rows[0].error == "not run: declined"
