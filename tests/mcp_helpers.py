"""Shared pieces of the MCP client tests (ADR-0091): the fixture server's
entry in a sandboxed ~/.silica/mcp.json, and a scripted model."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from silica.agent.llm import ToolCall

FIXTURE = str(Path(__file__).with_name("mcp_fixture_server.py"))


def configure(**servers) -> None:
    """Write ~/.silica/mcp.json (sandboxed by conftest) with stdio entries
    that start the fixture server, unless an entry names its own command."""
    from silica.connectors import mcp_config

    entries = {}
    for key, spec in servers.items():
        spec = dict(spec or {})
        entries[key] = {"command": spec.pop("command", sys.executable),
                        "args": spec.pop("args", [FIXTURE]), **spec}
    mcp_config.path().parent.mkdir(parents=True, exist_ok=True)
    mcp_config.path().write_text(json.dumps({"mcpServers": entries}), encoding="utf-8")


def call(name, args=None, cid="c1"):
    tc = ToolCall(id=cid, name=name, args=args or {})
    msg = {"role": "assistant", "tool_calls": [{"id": cid, "type": "function",
           "function": {"name": name, "arguments": json.dumps(args or {})}}]}
    return SimpleNamespace(assistant_message=msg, tool_calls=[tc], text=None, reasoning=None, usage={})


def text(t):
    return SimpleNamespace(assistant_message={"role": "assistant", "content": t}, tool_calls=[],
                           text=t, reasoning=None, usage={})


def scripted(*responses):
    """A call_llm double answering from a queue; `.offered` records the tool
    names each request carried, `.seen` the messages it was sent."""
    queue = list(responses)

    def fake(model, messages, **kw):
        fake.offered.append([s["function"]["name"] for s in kw.get("tools") or []])
        fake.seen.append([dict(m) for m in messages])
        return queue.pop(0)
    fake.offered, fake.seen = [], []
    return fake
