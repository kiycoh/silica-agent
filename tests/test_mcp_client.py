"""Silica as an MCP client (ADR-0091): each configured server is one loadable
tool, `mcp__<key>`, whose sub-loop alone holds that server's tools, and every
call that leaves the vault waits for the user's yes.

The servers here are synthetic, built on the 2.x SDK's own lowlevel Server
(tests/mcp_fixture_server.py); no real account is touched.
"""
from __future__ import annotations

import json
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from silica.agent.constraints import AgentConstraints
from silica.agent.llm import ToolCall
from silica.agent.loop import run_agent
from silica.tools import Tool
from tests.mcp_helpers import configure


class _NoArgs(BaseModel):
    pass


def _call(name, args=None, cid="c1"):
    tc = ToolCall(id=cid, name=name, args=args or {})
    msg = {"role": "assistant", "tool_calls": [{"id": cid, "type": "function",
           "function": {"name": name, "arguments": json.dumps(args or {})}}]}
    return SimpleNamespace(assistant_message=msg, tool_calls=[tc], text=None, reasoning=None, usage={})


def _text(t):
    return SimpleNamespace(assistant_message={"role": "assistant", "content": t}, tool_calls=[],
                           text=t, reasoning=None, usage={})


def _fake_llm(*responses):
    queue, offered = list(responses), []

    def fake(model, messages, **kw):
        offered.append([s["function"]["name"] for s in kw.get("tools") or []])
        return queue.pop(0)
    return fake, offered


# --- the two seams the client needs from the loop and the memory gate --------

def test_a_sub_loop_dispatches_from_its_own_registry_not_the_global_one():
    """A connector's tools live in its sub-loop only: registering them in TOOLS
    would put one server's names beside every other's and the vault's."""
    ran = []
    inner = Tool(lambda: ran.append(1) or '{"ok": true}', "send_message", "stub", _NoArgs, "atomic")
    fake, offered = _fake_llm(_call("send_message"), _text("done"))
    msgs = [{"role": "user", "content": "q"}]
    with patch("silica.agent.loop.call_llm", fake):
        run_agent(msgs, "test", constraints=AgentConstraints(
            tools=("send_message",), registry={"send_message": inner}))
    assert ran == [1]
    assert offered[0] == ["send_message"]


def test_a_connector_call_taints_the_conversation_for_user_memory():
    from silica.tools import user_memory

    turn = user_memory.begin_turn([{"role": "user", "content": "q"}], chat=True)
    user_memory.after_tool(turn, "mcp__gmail")
    assert turn.tainted_by == "mcp__gmail"


def test_a_connector_call_in_history_taints_the_next_turn():
    from silica.tools import user_memory

    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "tool_calls": [{"id": "1", "type": "function",
         "function": {"name": "mcp__github", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "x"},
        {"role": "user", "content": "remember that"},
    ]
    assert user_memory.begin_turn(history, chat=True).tainted_by == "mcp__github"


# --- the client: one event-loop thread, sessions held open per server --------

@pytest.fixture(autouse=True)
def _close_clients():
    yield
    from silica.connectors import mcp_client
    mcp_client.CLIENTS.close_all()


def test_a_stdio_server_lists_its_tools_and_answers_a_call():
    from silica.connectors import mcp_client

    configure(mail={})
    names = [t.name for t in mcp_client.CLIENTS.list_tools("mail")]
    assert "send_message" in names and "search_threads" in names
    res = mcp_client.CLIENTS.call("mail", "search_threads", {"query": "thesis"})
    assert "Thesis draft" in res.content[0].text


def test_a_listing_is_cached_for_the_catalog_of_the_next_process():
    from silica.connectors import mcp_client

    configure(mail={})
    assert mcp_client.cached_tools("mail") == []
    mcp_client.CLIENTS.list_tools("mail")
    cached = mcp_client.cached_tools("mail")
    assert [t["name"] for t in cached][:2] == ["search_threads", "send_message"]
    assert cached[0]["inputSchema"]["required"] == ["query"]


def test_a_secret_reaches_the_child_env_and_never_silicas_environment(monkeypatch):
    from silica.connectors import mcp_client, mcp_config

    monkeypatch.setenv("SILICA_LEAK_PROBE", "silica-only")
    configure(mail={"env": {"FIXTURE_TOKEN": "${keyring:FIXTURE_TOKEN}"}})
    mcp_config.set_secret("mail", "FIXTURE_TOKEN", "tok-123")
    res = mcp_client.CLIENTS.call("mail", "env_probe", {"names": ["FIXTURE_TOKEN", "SILICA_LEAK_PROBE"]})
    assert json.loads(res.content[0].text) == {"FIXTURE_TOKEN": "tok-123", "SILICA_LEAK_PROBE": None}
    assert "tok-123" not in os.environ.values()


def test_a_call_that_outlives_its_timeout_fails_as_timed_out():
    from silica.connectors import mcp_client

    configure(mail={})
    with pytest.raises(mcp_client.CallFailed, match="timed out"):
        mcp_client.CLIENTS.call("mail", "slow", {"seconds": 5}, timeout=0.5)


def test_a_cancelled_turn_stops_waiting_on_the_server():
    from silica.connectors import mcp_client

    configure(mail={})
    mcp_client.CLIENTS.list_tools("mail")  # connected: time only the call
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    t0 = time.monotonic()
    with pytest.raises(mcp_client.CallFailed, match="cancelled"):
        mcp_client.CLIENTS.call("mail", "slow", {"seconds": 10}, cancel=cancel)
    assert time.monotonic() - t0 < 3


def test_a_server_that_cannot_start_answers_unreachable():
    from silica.connectors import mcp_client

    configure(mail={"command": "/nonexistent/mcp-server", "args": []})
    with pytest.raises(mcp_client.CallFailed, match="unreachable"):
        mcp_client.CLIENTS.call("mail", "search_threads", {"query": "x"})


def test_an_entry_that_needs_a_secret_says_which_before_starting():
    from silica.connectors import mcp_client

    configure(mail={"env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}})
    with pytest.raises(mcp_client.CallFailed, match="needs GITHUB_TOKEN"):
        mcp_client.CLIENTS.call("mail", "search_threads", {"query": "x"})


def test_a_server_that_died_after_opening_fails_as_callfailed_then_reopens():
    """A stdio child gone between two calls: the next listing fails as a
    CallFailed, never a raw SDK error into the turn, and the one after opens
    a fresh session."""
    import signal
    import subprocess

    from silica.connectors import mcp_client
    from tests.mcp_helpers import FIXTURE

    configure(mail={"args": [FIXTURE, "--calls", os.devnull]})
    mcp_client.CLIENTS.list_tools("mail")
    pids = subprocess.run(["pgrep", "-f", f"{FIXTURE} --calls {os.devnull}"],
                          capture_output=True, text=True).stdout.split()
    assert pids, "the fixture child is running"
    for pid in pids:
        os.kill(int(pid), signal.SIGKILL)
    time.sleep(0.5)
    try:
        mcp_client.CLIENTS.list_tools("mail", timeout=10)
    except mcp_client.CallFailed:
        pass  # the death reported as a CallFailed is the contract under test
    names = [t.name for t in mcp_client.CLIENTS.list_tools("mail", timeout=20)]
    assert names[:1] == ["search_threads"]


# --- the e2e tester's defects (2026-10-12): a server that never answers -------

def _alive(pid: int) -> bool:
    """A process that has not exited; a zombie waiting for its reaper counts
    as gone, since it runs nothing."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        stat = open(f"/proc/{pid}/stat", encoding="ascii").read()
    except OSError:
        return True  # no /proc here: kill(0) answering is all there is
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


# The session, as a real process: open the servers in the background as the
# TUI and the GUI do, wait for the child to be up, then end the way the
# tester's runs did, by the interpreter exiting (TUI /exit, a scripted exit,
# the GUI after SIGINT all leave through atexit).
_EXITING_SESSION = """
import json, sys, time
from pathlib import Path

from silica.connectors import mcp_config, mcp_tools

pidfile, fixture, flags = Path(sys.argv[1]), sys.argv[2], sys.argv[3:]
mcp_config.path().parent.mkdir(parents=True, exist_ok=True)
mcp_config.path().write_text(json.dumps({"mcpServers": {"hang": {
    "command": sys.executable, "args": [fixture, *flags, "--pidfile", str(pidfile)]}}}))
mcp_tools.start()
deadline = time.monotonic() + 30
while not pidfile.exists() and time.monotonic() < deadline:
    time.sleep(0.05)
time.sleep(0.3)
print("child up" if pidfile.exists() else "no child", flush=True)
"""


@pytest.mark.parametrize("flags", [["--hang"], ["--hang", "--ignore-term"]],
                         ids=["hung", "hung-and-ignoring-sigterm"])
def test_a_hung_stdio_child_does_not_outlive_the_session(tmp_path, flags):
    import signal
    import subprocess
    import sys

    from tests.mcp_helpers import FIXTURE

    script, pidfile = tmp_path / "session.py", tmp_path / "child.pid"
    script.write_text(_EXITING_SESSION, encoding="utf-8")
    env = {**os.environ, "HOME": str(tmp_path / "home"),
           # The session reads no secret, but never the developer's keyring.
           "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"}
    out = subprocess.run([sys.executable, str(script), str(pidfile), FIXTURE, *flags],
                         env=env, cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert "child up" in out.stdout, out.stderr[-2000:]
    pid = int(pidfile.read_text().split()[0])
    try:
        deadline = time.monotonic() + 3  # the reaper's moment, if it was re-parented dead
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(pid), "the hung server outlived the session"
    finally:
        try:
            os.killpg(pid, signal.SIGKILL)  # a failed run leaves nothing behind either
        except (ProcessLookupError, PermissionError):
            pass


def test_a_handshake_that_never_completes_reads_failed_after_the_open_timeout(monkeypatch, tmp_path):
    """Before the fix the row read "not connected yet" for as long as the
    process lived (tester, 2026-10-12)."""
    from silica.connectors import find, mcp_client
    from tests.mcp_helpers import FIXTURE

    monkeypatch.setattr(mcp_client, "OPEN_TIMEOUT_S", 1.0, raising=False)
    pidfile = tmp_path / "child.pid"
    configure(hang={"args": [FIXTURE, "--hang", "--pidfile", str(pidfile)]})
    mcp_client.CLIENTS.warm(["hang"])
    deadline = time.monotonic() + 20
    state = ""
    while time.monotonic() < deadline:
        state = find("mcp").status()["rows"][0]["state"]
        if not state.startswith("not connected"):
            break
        time.sleep(0.1)
    assert state.startswith("failed: ") and "1 s" in state, state
    # The session it gave up on took its child with it.
    pid = int(pidfile.read_text().split()[0])
    deadline = time.monotonic() + 10
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(pid)


def test_a_call_on_a_server_that_never_answers_fails_at_the_open_timeout(monkeypatch):
    from silica.connectors import mcp_client
    from tests.mcp_helpers import FIXTURE

    monkeypatch.setattr(mcp_client, "OPEN_TIMEOUT_S", 1.0, raising=False)
    configure(hang={"args": [FIXTURE, "--hang"]})
    t0 = time.monotonic()
    with pytest.raises(mcp_client.CallFailed, match="did not answer within 1 s"):
        mcp_client.CLIENTS.list_tools("hang", timeout=30)
    assert time.monotonic() - t0 < 15


def test_each_stdio_child_is_known_to_its_session():
    """The kill of last resort needs the child: the SDK's stdio transport
    keeps it to itself, and a bump that renamed the spawn hook would leave
    only its own teardown. This pins the hook."""
    from silica.connectors import mcp_client
    from tests.mcp_helpers import FIXTURE

    configure(mail={"args": [FIXTURE, "--calls", os.devnull]})
    mcp_client.CLIENTS.list_tools("mail")
    procs = mcp_client.CLIENTS._sessions["mail"].procs
    assert len(procs) == 1 and procs[0].returncode is None and _alive(procs[0].pid)


def test_a_child_still_alive_after_the_close_bound_is_killed(monkeypatch, tmp_path):
    """The SDK's teardown of a child that ignores SIGTERM takes ~4.5 s; a
    close bounded below that kills it itself."""
    from silica.connectors import mcp_client
    from tests.mcp_helpers import FIXTURE

    monkeypatch.setattr(mcp_client, "CLOSE_TIMEOUT_S", 0.5)
    pidfile = tmp_path / "child.pid"
    configure(hang={"args": [FIXTURE, "--hang", "--ignore-term", "--pidfile", str(pidfile)]})
    mcp_client.CLIENTS.warm(["hang"])
    deadline = time.monotonic() + 20
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    pid = int(pidfile.read_text().split()[0])
    t0 = time.monotonic()
    mcp_client.CLIENTS.close("hang")
    assert time.monotonic() - t0 < 3
    deadline = time.monotonic() + 2
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(pid)
