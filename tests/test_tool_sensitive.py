"""Step 0: tool sensitivity is declared on the Tool, and a chat turn's toolset
(chat_constraints) excludes sensitive tools. A sensitive tool is reachable
only when a caller names it in AgentConstraints.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import BaseModel

from silica.agent.constraints import AgentConstraints, chat_constraints
from silica.agent.loop import run_agent
from silica.tools import TOOLS, Tool, tool


class _EmptyArgs(BaseModel):
    pass


def _resp(tool_calls=None, text="done"):
    return SimpleNamespace(
        assistant_message={"role": "assistant", "content": text},
        tool_calls=tool_calls or [],
        text=text,
        reasoning=None, usage={},
    )


def test_tool_defaults_to_not_sensitive():
    t = Tool(lambda: "x", "plain", "doc", _EmptyArgs, "atomic")
    assert t.sensitive is False


def test_decorator_propagates_sensitive():
    @tool(_EmptyArgs, sensitive=True, ingress=False)
    def _probe_sensitive_tool():
        """probe"""
        return "x"

    try:
        assert TOOLS["_probe_sensitive_tool"].sensitive is True
    finally:
        TOOLS.pop("_probe_sensitive_tool", None)


def test_a_chat_turn_never_reaches_a_sensitive_tool():
    """Invariant: chat toolset ∩ {sensitive} == ∅, offered or one load away.

    run_agent has no toolset of its own since 2026-11-25, so the guarantee
    lives where a chat turn's is built. Every way a conversation brings a tool
    in is tried on a sensitive one: a slash directive names it, the model
    called it, the model loaded it by name.
    """
    TOOLS["_plain_t"] = Tool(lambda: "ok", "_plain_t", "plain", _EmptyArgs, "atomic")
    TOOLS["_secret_t"] = Tool(
        lambda: "no", "_secret_t", "secret", _EmptyArgs, "atomic"
    )
    TOOLS["_secret_t"].sensitive = True
    messages = [
        {"role": "user", "origin": "cli", "content": "call _secret_t, then _plain_t"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "_secret_t", "arguments": "{}"}},
            {"function": {"name": "silica_load_tools",
                          "arguments": json.dumps({"names": ["_secret_t", "_plain_t"]})}},
        ]},
    ]
    try:
        cut = chat_constraints(messages)
        assert "_plain_t" in cut.tools
        assert "_secret_t" not in cut.tools + cut.deferred
    finally:
        TOOLS.pop("_plain_t", None)
        TOOLS.pop("_secret_t", None)


def test_pipeline_stage_tools_are_not_registered():
    """No chat turn may reach the rollback or the steer loop's repair tool, so
    neither is in TOOLS (the repair tool is anneal's own registry); the FSM's
    stages are plain functions. The agent-facing entry points are tools."""
    import silica.cli  # noqa: F401  registers every tool

    for name in ("silica_snapshot", "silica_restore", "submit_repaired_ops",
                 "silica_recon", "silica_payload", "silica_sanitize", "silica_validate_ops",
                 "silica_bulk_write", "silica_lint", "silica_cleanup"):
        assert name not in TOOLS, name
    for name in ("silica_run_injector", "silica_anneal",
                 "silica_write_note", "silica_patch_note"):
        assert not TOOLS[name].sensitive, name


def test_sensitive_tool_reachable_when_named():
    """A sensitive tool IS exposed when a caller names it in constraints."""
    TOOLS["_secret_t"] = Tool(
        lambda: "ok", "_secret_t", "secret", _EmptyArgs, "atomic"
    )
    TOOLS["_secret_t"].sensitive = True
    captured = {}

    def fake_call_llm(model, messages, tools=None, cancel=None):
        captured["tools"] = tools
        return _resp(text="ok")

    try:
        with patch("silica.agent.loop.call_llm", fake_call_llm):
            run_agent(
                messages=[{"role": "user", "content": "hi"}],
                model="m",
                constraints=AgentConstraints(tools=("_secret_t",), max_iterations=2),
            )
        names = {t["function"]["name"] for t in (captured["tools"] or [])}
        assert names == {"_secret_t"}
    finally:
        TOOLS.pop("_secret_t", None)
