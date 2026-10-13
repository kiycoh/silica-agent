# SPDX-License-Identifier: AGPL-3.0-or-later
"""ADR-0100 points 2 and 3: a hosted tenant's journal keeps templates, never
arguments, and the usage lines carry numbers and code-defined names only."""
from __future__ import annotations

import asyncio
import collections
import json
import logging

import pytest

from silica.agent.events import LLMStreamEvent, ToolErrorEvent, ToolStartEvent
from silica.ui.logging import HostedJsonFormatter

MARKER = "student-wrote-this-7f3a"


class _Lines(logging.Handler):
    """Formats every record the way the hosted handler does."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.setFormatter(HostedJsonFormatter())
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(self.format(record))

    def events(self, name: str) -> list[dict]:
        return [d for d in map(json.loads, self.lines) if d.get("event") == name]


@pytest.fixture
def journal(monkeypatch):
    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.delenv("SILICA_METRICS_OPTOUT", raising=False)
    log = logging.getLogger("silica.metrics")
    saved = log.level
    log.setLevel(logging.INFO)
    lines = _Lines()
    log.addHandler(lines)
    yield lines
    log.removeHandler(lines)
    log.setLevel(saved)


def _record(name: str, msg, args=(), exc: BaseException | None = None) -> logging.LogRecord:
    exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
    return logging.LogRecord(name, logging.ERROR, __file__, 1, msg, args, exc_info)


def test_no_argument_or_exception_text_reaches_the_hosted_journal():
    fmt = HostedJsonFormatter()
    try:
        raise ValueError(f"cannot parse {MARKER}")
    except ValueError as e:
        caught = e
    lines = [
        fmt.format(_record("silica.agent.loop", "Tool call: %s(%s)", ("read_note", MARKER))),
        fmt.format(_record("silica.ui.web.server", "web turn failed", exc=caught)),
        fmt.format(_record("silica.agent.loop", caught)),  # logger.warning(exc)
        # litellm's own calls are f-strings with the exception's text inside.
        fmt.format(_record("LiteLLM", f"Error in is_prompt_caching_valid_prompt: {MARKER}", exc=caught)),
    ]
    assert all(MARKER not in line for line in lines)
    tool, failed, bare, third = map(json.loads, lines)
    assert tool["msg"] == "Tool call: %s(%s)" and tool["level"] == "ERROR"
    assert failed["exc"]["type"] == "ValueError"
    assert failed["exc"]["frames"][-1].endswith(":test_no_argument_or_exception_text_reaches_the_hosted_journal:"
                                                f"{caught.__traceback__.tb_lineno}")
    assert bare["msg"] == "<ValueError>"
    assert "msg" not in third and third["logger"] == "LiteLLM" and third["exc"]["type"] == "ValueError"


def test_hosted_setup_writes_json_and_lets_only_metrics_through_below_warning(monkeypatch, capsys):
    import sys
    from silica.cli import _setup_logging
    from silica.config import CONFIG
    from silica.metrics import emit

    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.delenv("SILICA_METRICS_OPTOUT", raising=False)
    orig = CONFIG.debug_logging
    try:
        _setup_logging(debug=True)  # hosted wins over --verbose
        logging.getLogger("silica.probe").info("info-%s", MARKER)
        logging.getLogger("silica.probe").warning("warn %s", MARKER)
        emit("turn", kind="chat", outcome="done", ms=12)
        sys.stderr.flush()
        err = capsys.readouterr().err
    finally:
        monkeypatch.delenv("SILICA_HOSTED")
        _setup_logging(debug=orig)
    rows = [json.loads(line) for line in err.splitlines() if line.startswith("{")]
    assert MARKER not in err
    assert {"logger": "silica.probe", "level": "WARNING", "msg": "warn %s"}.items() <= rows[0].items()
    assert {"event": "turn", "kind": "chat", "outcome": "done", "ms": 12}.items() <= rows[1].items()
    assert len(rows) == 2  # the INFO record never left the logger


def test_emit_is_silent_off_the_hosted_service_and_after_an_objection(journal, monkeypatch):
    from silica.metrics import emit

    emit("turn", kind="chat")
    monkeypatch.setenv("SILICA_METRICS_OPTOUT", "1")
    emit("turn", kind="slash")
    monkeypatch.delenv("SILICA_METRICS_OPTOUT")
    monkeypatch.delenv("SILICA_HOSTED")
    emit("turn", kind="find")
    assert [e["kind"] for e in journal.events("turn")] == ["chat"]


def test_each_model_call_writes_an_llm_call_line(journal, monkeypatch):
    from silica.agent import llm

    monkeypatch.setattr(llm, "_METER_ON", True)
    monkeypatch.setattr(llm, "_meter", collections.defaultdict(lambda: [0, 0, 0, 0]))  # no exit table
    before = llm.meter_totals()

    def _distill():
        return llm.LLMResponse(
            usage={"prompt_tokens": 100, "completion_tokens": 10, "cost": 0.0004,
                   "prompt_tokens_details": {"cached_tokens": 40}},
            finish_reason="stop", request={"model": "openrouter/z-ai/glm-5.3-flash"})

    _distill()
    (line,) = journal.events("llm_call")
    assert line == {"ts": line["ts"], "event": "llm_call", "site": "test_pilot_metrics.py:_distill",
                    "model": "openrouter/z-ai/glm-5.3-flash", "prompt": 100, "cached": 40,
                    "completion": 10, "finish": "stop", "usd": 0.0004}
    after = llm.meter_totals()
    assert [a - b for a, b in zip(after, before)] == [1, 100, 10, 40]


def test_turn_kind_names_only_built_in_commands():
    from silica.ui.web.server import _turn_kind

    assert _turn_kind("what do my notes say?") == "chat"
    assert _turn_kind("/find bayes --k=3") == "find"
    assert _turn_kind("/FIND bayes") == "find"
    assert _turn_kind("/my-own-skill do it") == "slash"


@pytest.fixture
def client(tmp_vault, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from silica.ui.web import server

    server._reset_session()
    return TestClient(server.app), server


def test_a_chat_turn_writes_one_turn_line(client, journal, monkeypatch):
    from silica.agent import llm

    _, server = client
    monkeypatch.setattr(llm, "_METER_ON", True)
    monkeypatch.setattr(llm, "_meter", collections.defaultdict(lambda: [0, 0, 0, 0]))

    def fake_run_agent(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
        tool_progress_callback(ToolStartEvent("search_context", {"query": MARKER}, "c1", 0))
        tool_progress_callback(ToolErrorEvent("read_note", "c2", MARKER, 0))
        tool_progress_callback(ToolStartEvent("search_context", {"query": "x"}, "c3", 0))
        llm.LLMResponse(usage={"prompt_tokens": 30, "completion_tokens": 5})
        tool_progress_callback(LLMStreamEvent("text", "Hi", 1))
        messages.append({"role": "assistant", "content": "Hi"})
        return "Hi"

    monkeypatch.setattr(server, "run_agent", fake_run_agent)

    async def collect():
        return [item async for item in server.run_turn(f"tell me about {MARKER}")]

    asyncio.run(collect())
    (turn,) = journal.events("turn")
    assert MARKER not in json.dumps(turn)
    assert turn["kind"] == "chat" and turn["outcome"] == "done"
    assert turn["tools"] == {"search_context": 2} and turn["tool_errors"] == {"read_note": 1}
    assert (turn["calls"], turn["prompt"], turn["completion"], turn["cached"]) == (1, 30, 5, 0)
    assert isinstance(turn["ms"], int) and isinstance(turn["first_token_ms"], int)


def test_an_empty_answer_and_an_unknown_slash_are_named_as_such(client, journal, monkeypatch):
    from silica.agent.loop import _EMPTY_COMPLETION

    _, server = client
    monkeypatch.setattr(server, "run_agent", lambda messages, *a, **k: messages.append(
        {"role": "assistant", "content": _EMPTY_COMPLETION}) or _EMPTY_COMPLETION)

    async def collect(text):
        return [item async for item in server.run_turn(text)]

    asyncio.run(collect("hello"))
    asyncio.run(collect("/definitely-not-a-command"))
    first, second = journal.events("turn")
    assert (first["kind"], first["outcome"], first["first_token_ms"]) == ("chat", "empty", None)
    assert (second["kind"], second["outcome"]) == ("slash", "error")


def test_an_upload_writes_an_ingest_line_without_its_name(client, journal, monkeypatch):
    tc, server = client
    monkeypatch.setattr(server, "run_agent", lambda messages, *a, **kw: "ok")
    body = b"# Hi\n\nsome body text to stage"
    resp = tc.post("/nucleate", files=[("files", (f"{MARKER}.md", body, "text/markdown")),
                                       ("files", (f"x.{MARKER}", b"?", "application/octet-stream"))])
    assert resp.status_code == 200
    lines = journal.events("ingest")
    assert MARKER not in json.dumps(lines)
    assert lines[0] == {"ts": lines[0]["ts"], "event": "ingest", "ext": ".md", "bytes": len(body),
                        "pages": None, "pages_without_text": None, "outcome": "ok"}
    assert lines[1]["ext"] == "other"
