# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The MCP client: Silica's sessions with the servers in ~/.silica/mcp.json.

On the 2.x SDK from its first line (ADR-0091 point 1): `Client` in its
default `auto` mode probes `server/discover` and falls back to `initialize`,
so it reaches servers of either protocol era. The SDK is async and the agent
loop is not, so every session lives on one event-loop thread owned by this
module; a tool call submits to it and waits, at most 60 seconds, and stops
waiting the moment the turn's cancel token is set (ADR-0004).

A session is held open by a task of its own: anyio ties a client's cancel
scope to the task that entered it, so opening in one call and closing in
another would raise. Calls from other tasks share the open session.

The `mcp` SDK is imported lazily: without the [mcp] extra this module still
imports and every call answers that the extra is missing.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import datetime as _dt
import json
import logging
import os
import signal
import threading
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

from silica.connectors import mcp_config

logger = logging.getLogger(__name__)

CALL_TIMEOUT_S = 60.0
# How long the background warm-up waits for a server's first listing. ADR-0091
# names 5 s for startup; the session itself is not given up at 5 s, below.
WARM_OPEN_TIMEOUT_S = 5.0
# How long a session may take to open before it is given up, its child goes
# and its row reads failed. A call's whole budget and not the warm-up's 5 s: a
# first `npx` download of a server takes longer than that. A login opens for
# as long as it waits for the person instead (mcp_oauth.LOGIN_TIMEOUT_S).
OPEN_TIMEOUT_S = CALL_TIMEOUT_S
# How long a close waits for the SDK's own teardown of a stdio child before
# killing it: stdin closed, 2 s grace, SIGTERM to its group, 2 s, SIGKILL, 2 s
# to reap is 6.5 s at worst (mcp/client/stdio.py, 2.3.0). Exiting before the
# teardown ran is how a hung server outlived the TUI's /exit (tester,
# 2026-10-12). Re-measure if a minor bump changes those constants.
CLOSE_TIMEOUT_S = 8.0


class CallFailed(RuntimeError):
    """A call that did not reach an answer; the message is the reason,
    already scrubbed of credentials."""


class _OpenTimeout(TimeoutError):
    """A session that did not open within its bound; the message says which."""


def _scrub(text: object) -> str:
    from silica.kernel.scrub import scrub_credentials

    return scrub_credentials(text)


# --- the tool listing cache --------------------------------------------------

def _cache_path() -> Path:
    from silica.kernel.recall import paths

    return paths._SILICA_HOME / "connectors" / "mcp-tools.json"


def _read_cache() -> dict[str, Any]:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        # A torn cache costs the catalog its tool names until the next
        # listing rewrites it; the connector itself still works.
        logger.warning("mcp tool cache unreadable: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def cached_tools(key: str) -> list[dict[str, Any]]:
    """The server's tools as of its last successful listing, in wire shape."""
    entry = _read_cache().get(key)
    tools = entry.get("tools") if isinstance(entry, dict) else None
    return [t for t in tools if isinstance(t, dict) and t.get("name")] if isinstance(tools, list) else []


def _write_cache(key: str, tools: list[dict[str, Any]]) -> None:
    from silica.kernel.recall.paths import atomic_write_bytes

    data = _read_cache()
    data[key] = {"tools": tools, "listed": _dt.datetime.now().isoformat(timespec="seconds")}
    _cache_path().parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(_cache_path(), json.dumps(data, indent=1).encode("utf-8"))


def forget_cache(key: str) -> None:
    data = _read_cache()
    if data.pop(key, None) is not None:
        from silica.kernel.recall.paths import atomic_write_bytes

        atomic_write_bytes(_cache_path(), json.dumps(data, indent=1).encode("utf-8"))


# --- transports --------------------------------------------------------------

def _errlog(key: str):
    """Where a stdio server's stderr goes: its own log, never the terminal the
    TUI is drawing on."""
    from silica.kernel.recall import paths

    folder = paths._SILICA_HOME / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    safe = mcp_config.tool_name(key)[len("mcp__"):]
    return open(folder / f"mcp-{safe}.log", "a", encoding="utf-8")  # noqa: SIM115  closed with the session


# The stdio children the session task running now has started, for the kill
# of last resort (Clients._close). Task-local: create_task copies the context,
# so each session's task sees its own list.
_SPAWNED: contextvars.ContextVar[list[Any] | None] = contextvars.ContextVar("mcp_spawned", default=None)


def _record_spawns() -> None:
    """Have the SDK's stdio transport hand each child it starts to the session
    that started it. stdio_client keeps the process to itself, and a close
    that cannot wait out its teardown must still be able to kill it; the
    spawn function it calls by module name is the one place the process is
    seen. Private SDK API, under the `<3` pin that reruns Gate 0 on every
    minor bump: if a bump renames it, the hook is skipped and the SDK's own
    teardown, which CLOSE_TIMEOUT_S waits out, is the only kill left."""
    import mcp.client.stdio as sdk_stdio

    spawn = getattr(sdk_stdio, "_create_platform_compatible_process", None)
    if spawn is None or getattr(spawn, "_silica_records", False):
        return

    async def recording(*args: Any, **kwargs: Any) -> Any:
        process = await spawn(*args, **kwargs)
        procs = _SPAWNED.get()
        if procs is not None:
            procs.append(process)
        return process

    recording._silica_records = True  # type: ignore[attr-defined]
    sdk_stdio._create_platform_compatible_process = recording


def _kill_survivors(procs: list[Any]) -> None:
    """SIGKILL the group of every child not yet reaped. Only one whose
    returncode is still unset: until it is reaped its pid cannot be reused, so
    the group signalled is the child's own (start_new_session makes pid ==
    pgid)."""
    if not hasattr(os, "killpg"):
        return  # Windows: the SDK's Job Object ends the tree with the session
    for process in procs:
        if getattr(process, "returncode", 0) is not None:
            continue
        try:
            os.killpg(process.pid, signal.SIGKILL)
            logger.warning("mcp: killed server pid %d, still alive after its close", process.pid)
        except (ProcessLookupError, PermissionError):
            pass  # gone between the check and the kill, or no longer ours to signal


def _transport(key: str, spec: dict[str, Any], auth: Any):
    """The transport context manager for a resolved entry."""
    if "command" in spec:
        from mcp.client.stdio import StdioServerParameters, stdio_client

        _record_spawns()

        @asynccontextmanager
        async def stdio():
            log = _errlog(key)
            try:
                # `env` is merged over the SDK's six inherited variables (HOME,
                # LOGNAME, PATH, SHELL, TERM, USER), never Silica's whole
                # environment: a secret reaches only the child that needs it.
                params = StdioServerParameters(command=spec["command"], args=spec["args"],
                                               env=spec["env"] or None)
                async with stdio_client(params, errlog=log) as streams:
                    yield streams
            finally:
                log.close()

        return stdio()

    import httpx2
    from mcp.client.streamable_http import streamable_http_client

    @asynccontextmanager
    async def http():
        # The SDK's own defaults: 30 s to connect, 300 s to read a held stream.
        async with httpx2.AsyncClient(headers=spec["headers"] or None, auth=auth,
                                      timeout=httpx2.Timeout(30.0, read=300.0)) as client:
            async with streamable_http_client(spec["url"], http_client=client) as streams:
                yield streams

    return http()


# --- sessions ----------------------------------------------------------------

class _Session:
    def __init__(self, key: str) -> None:
        self.key = key
        self.client: Any = None
        self.ready = asyncio.Event()
        self.closing = asyncio.Event()
        self.error: BaseException | None = None
        self.task: asyncio.Task | None = None
        self.procs: list[Any] = []  # the stdio children it started (_record_spawns)

    @property
    def alive(self) -> bool:
        return self.client is not None and self.error is None and not self.closing.is_set()


class Clients:
    """Every open session, on one event-loop thread."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}
        # The last reason a server failed to open or answer, scrubbed: what
        # its Settings row reads after "failed:". Cleared when it opens.
        self.errors: dict[str, str] = {}
        # Set by mcp_oauth when a login is in progress, so a call never opens
        # a browser mid-turn (the redirect handler refuses outside a login).
        self.interactive_login: set[str] = set()
        # Set at the process's exit: a session the warm-up would open after
        # the exit's close ran would leave a child nobody closes.
        self._shut = False

    # -- the loop thread --
    def _get_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None or self._loop.is_closed():
                loop = asyncio.new_event_loop()
                threading.Thread(target=loop.run_forever, name="mcp-client", daemon=True).start()
                self._loop = loop
            return self._loop

    def _wait(self, coro, timeout: float, cancel: threading.Event | None) -> Any:
        fut = asyncio.run_coroutine_threadsafe(coro, self._get_loop())
        deadline = time.monotonic() + timeout
        while True:
            try:
                return fut.result(timeout=0.2)
            except concurrent.futures.TimeoutError:
                if cancel is not None and cancel.is_set():
                    fut.cancel()
                    raise CallFailed("cancelled") from None
                if time.monotonic() >= deadline:
                    fut.cancel()
                    raise CallFailed(f"timed out after {timeout:g} s") from None
            except concurrent.futures.CancelledError:
                raise CallFailed("cancelled") from None

    # -- in the loop --
    def _open_timeout(self, key: str) -> float:
        if key in self.interactive_login:
            from silica.connectors import mcp_oauth

            return mcp_oauth.LOGIN_TIMEOUT_S + 30
        return OPEN_TIMEOUT_S

    async def _hold(self, s: _Session, spec: dict[str, Any], open_s: float) -> None:
        from mcp.client import Client

        from silica.connectors import mcp_oauth

        _SPAWNED.set(s.procs)
        auth = mcp_oauth.provider(s.key, spec, self) if "url" in spec else None
        try:
            async with AsyncExitStack() as stack:
                try:
                    # Only the open is bounded, never the session it opens.
                    async with asyncio.timeout(open_s):
                        s.client = await stack.enter_async_context(
                            Client(_transport(s.key, spec, auth), mode="auto"))
                except TimeoutError:
                    raise _OpenTimeout(f"did not answer within {open_s:g} s") from None
                self.errors.pop(s.key, None)
                s.ready.set()
                await s.closing.wait()
        except BaseException as exc:  # noqa: BLE001  the session's end, reported to its callers
            s.error = exc
            # Recorded here and not by a waiter: the warm-up stops waiting at
            # 5 s, and a session nobody waits on read "not connected yet" for
            # as long as the process lived (tester, 2026-10-12). A close is
            # not a failure: it took the session out of the table first.
            if not s.ready.is_set() and self._sessions.get(s.key) is s \
                    and not isinstance(exc, asyncio.CancelledError):
                self.errors[s.key] = _describe_open_error(exc)
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            s.client = None
            s.ready.set()
            _kill_survivors(s.procs)

    async def _session(self, key: str) -> Any:
        if self._shut:
            raise CallFailed("closing: this session is ending")
        s = self._sessions.get(key)
        if s is not None and s.alive:
            return s.client
        if s is not None and not s.ready.is_set():
            await s.ready.wait()
            if s.alive:
                return s.client
        entry = mcp_config.load().get(key)
        if entry is None:
            raise CallFailed(f"no server named '{key}' in {mcp_config.path()}")
        try:
            spec = mcp_config.resolve(entry)
        except mcp_config.Unresolved as exc:
            raise CallFailed(str(exc)) from None
        s = _Session(key)
        self._sessions[key] = s
        s.task = asyncio.get_running_loop().create_task(self._hold(s, spec, self._open_timeout(key)))
        await s.ready.wait()
        if not s.alive:
            raise CallFailed(self.errors.get(key) or _describe_open_error(s.error))
        return s.client

    async def _list(self, key: str) -> list[Any]:
        import mcp.types as types

        client = await self._session(key)
        tools: list[Any] = []
        cursor = None
        try:
            for _page in range(20):  # a paginator that never ends cannot hang a listing
                res = await client.session.list_tools(
                    params=types.PaginatedRequestParams(cursor=cursor) if cursor else None)
                tools.extend(res.tools)
                cursor = res.next_cursor
                if not cursor:
                    break
        except Exception as exc:
            raise self._casualty(key, exc) from None
        _write_cache(key, [t.model_dump(by_alias=True, mode="json", exclude_none=True) for t in tools])
        return tools

    async def _call(self, key: str, tool: str, args: dict[str, Any]) -> Any:
        client = await self._session(key)
        try:
            return await client.call_tool(tool, args)
        except Exception as exc:
            raise self._casualty(key, exc) from None

    def _casualty(self, key: str, exc: Exception) -> CallFailed:
        """A request that failed on an open session. The session may be the
        casualty (a stdio server that exited), so it is dropped and the next
        request opens a fresh one; the caller gets a CallFailed, never the
        SDK's own error, which the turn would show raw."""
        s = self._sessions.get(key)
        if s is not None:
            s.closing.set()
        why = _scrub(f"{type(exc).__name__}: {exc}")
        self.errors[key] = why
        return CallFailed(why)

    async def _close(self, key: str) -> None:
        """End one session and wait for its child to be gone, within
        CLOSE_TIMEOUT_S; a child still there after that is killed."""
        s = self._sessions.pop(key, None)
        if s is None or s.task is None:
            return
        s.closing.set()  # an open session leaves its `async with`: the SDK's teardown runs
        if not s.ready.is_set():
            # Still opening (a server hung in its handshake): there is no
            # session to finish, and the SDK's teardown runs under the cancel.
            s.task.cancel()
        done, _ = await asyncio.wait({s.task}, timeout=CLOSE_TIMEOUT_S)
        if not done:
            logger.warning("mcp session %s did not close within %g s", key, CLOSE_TIMEOUT_S)
            s.task.cancel()
        _kill_survivors(s.procs)

    async def _close_many(self, keys: list[str]) -> None:
        # Together: N hung servers cost one CLOSE_TIMEOUT_S at exit, not N.
        await asyncio.gather(*(self._close(k) for k in keys))

    def _close_keys(self, keys: list[str]) -> None:
        if self._loop is None or not keys:
            return
        sessions = [s for k in keys if (s := self._sessions.get(k)) is not None]
        try:
            self._wait(self._close_many(keys), CLOSE_TIMEOUT_S + 2, None)
        except CallFailed as exc:
            # The loop itself did not finish the close in time (a wedged
            # loop thread cannot run the teardown): the kill below is the
            # close, from this thread.
            logger.warning("mcp close %s: %s", ", ".join(keys), exc)
        for s in sessions:
            _kill_survivors(s.procs)

    # -- the blocking API the tools use --
    def available(self) -> str:
        """"" when the SDK is importable, else why not."""
        try:
            import mcp.client  # noqa: F401
        except ImportError:
            return "needs the [mcp] extra: uv pip install 'silica-agent[mcp]'"
        return ""

    def list_tools(self, key: str, *, timeout: float = CALL_TIMEOUT_S,
                   cancel: threading.Event | None = None) -> list[Any]:
        if why := self.available():
            raise CallFailed(why)
        return self._wait(self._list(key), timeout, cancel)

    def call(self, key: str, tool: str, args: dict[str, Any], *, timeout: float = CALL_TIMEOUT_S,
             cancel: threading.Event | None = None) -> Any:
        if why := self.available():
            raise CallFailed(why)
        return self._wait(self._call(key, tool, args), timeout, cancel)

    def connected(self, key: str) -> bool:
        s = self._sessions.get(key)
        return s is not None and s.alive

    def close(self, key: str) -> None:
        self._close_keys([key])

    def close_all(self) -> None:
        """Close every session and forget why any failed: what reopens next
        reports its own state."""
        self._close_keys(list(self._sessions))
        self.errors.clear()

    def shutdown(self) -> None:
        """The process is exiting (atexit): close every session, children
        included, and open none after."""
        self._shut = True
        self.close_all()

    def warm(self, keys: list[str]) -> None:
        """Open every startable server in parallel, off the caller's thread,
        so the first call does not pay the start and the tool cache is fresh
        for the next process. Never blocks: startup does not wait on it."""
        if self.available() or self._shut:
            return
        for key in keys:
            # Bounded so a hung server leaves no waiter behind; the session's
            # own task keeps opening, and is there for the first call if it lands.
            fut = asyncio.run_coroutine_threadsafe(
                asyncio.wait_for(self._list(key), WARM_OPEN_TIMEOUT_S), self._get_loop())
            # A server that does not answer in time is left for the first call
            # to open; its row reads what the failure was.
            fut.add_done_callback(lambda f, k=key: f.cancelled() or f.exception() is None
                                  or logger.info("mcp warm %s: %s", k, f.exception()))


def _describe_open_error(exc: BaseException | None) -> str:
    """One scrubbed line for a session that never opened."""
    if exc is None:
        return "unreachable: the session closed before it opened"
    # anyio wraps a task group's failure; the first leaf says what happened.
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    from silica.connectors import mcp_oauth

    if isinstance(exc, _OpenTimeout):
        return f"unreachable: {exc}"
    if isinstance(exc, mcp_oauth.NeedsLogin):
        return "needs login"
    if isinstance(exc, mcp_oauth.IssuerMoved):
        # Said as the last registration step, so the row offers the field
        # for an ID registered with the new issuer.
        return _scrub(f"needs a client ID: {exc}")
    from silica.connectors import credentials

    if isinstance(exc, credentials.NoStore):
        # A login whose grant has nowhere to go: the row offers the file.
        return f"needs a credentials store: {exc}"
    if type(exc).__name__ == "OAuthRegistrationError":
        # The spec's last registration step: neither CIMD nor dynamic
        # registration worked, so the row asks for a hand-registered client ID.
        return _scrub(f"needs a client ID: {exc}")
    return _scrub(f"unreachable: {type(exc).__name__}: {exc}")


CLIENTS = Clients()
