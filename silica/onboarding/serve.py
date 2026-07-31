# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Start the local model servers named in the env, before anything needs them.

A local endpoint (LM Studio, llama.cpp, Ollama, vLLM) is a process someone has
to remember to start. Forgetting it is invisible: embeddings fall back to
co-occurrence and the rerank pass silently never runs, so answers stay plausible
and quietly get worse. When the env names the command that serves an endpoint,
silica runs that command itself and waits for it to load instead of degrading.

One key per endpoint (SILICA_*_SERVE_CMD), run through the shell so a script
path, `lms server start`, or a chained command all work as written. Unset means
"I start it myself" — the behaviour before this module, unchanged, and no probe.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
_READY_TIMEOUT = float(os.getenv("SILICA_SERVE_TIMEOUT", "180"))


def is_local(base_url: str) -> bool:
    """Whether this endpoint is one silica could start — a remote one is not ours."""
    url = urlparse(base_url if "//" in base_url else f"//{base_url}")
    return url.hostname in _LOCAL_HOSTS


def _ready(base_url: str) -> bool:
    """True when the endpoint answers as a *loaded* server.

    Deliberately not a port check: llama.cpp starts the HTTP server before it
    loads the model and answers 503 "Loading model" on every path meanwhile
    (tools/server/server-http.cpp, middleware_server_state). An open port would
    therefore mean "starting", and silica would fire the first rerank call into
    a 503 — the silent-degradation this module exists to prevent.

    Any other status counts as up: a server that answers at all is past loading,
    even one without a /models route.
    """
    import httpx

    try:
        return httpx.get(f"{base_url.rstrip('/')}/models", timeout=2.0).status_code != 503
    except Exception:
        return False


# The cause is rarely on the last line: llama.cpp prints "cleaning up before
# exit" after the error that killed it. Show the error lines when there are any.
_ERROR_LINE = re.compile(r"\b(error|failed|cannot|no such|denied|refused)\b", re.I)

# What the user loses when this endpoint never comes up. The whole point of the
# module is that silent degradation looks like success, so the failure names it.
_DEGRADES_TO = {
    "embeddings": "retrieval falls back to co-occurrence — recall drops",
    "rerank": "the rerank pass is skipped — worse ordering, no error",
    "chat": "no chat provider — silica cannot answer at all",
}


def _fail(label: str, headline: str, log_path: Path) -> bool:
    """Report a server that never came up: what broke, why, what it costs."""
    from silica.ui.console import CONSOLE

    # info, not warning: the WARNING handler writes to stderr too, and the red
    # block below is the loud channel — a warning here just prints it twice.
    logger.info("%s: %s — see %s", label, headline, log_path)
    CONSOLE.print(f"  [red]✗ {label}: {headline}[/]")
    try:  # the log's own error lines are the actual cause (bad model path, OOM…)
        lines = [ln for ln in log_path.read_text(errors="replace").splitlines() if ln.strip()]
    except OSError:
        lines = []
    hits = [ln for ln in lines if _ERROR_LINE.search(ln)]
    for line in (hits or lines)[-3:]:
        CONSOLE.print(f"    [dim]{line[:200]}[/]")
    if label in _DEGRADES_TO:
        CONSOLE.print(f"  [yellow]→ {_DEGRADES_TO[label]}.[/]")
    CONSOLE.print(f"  [dim]Full log: {log_path}[/]")
    return False


def ensure(label: str, base_url: str, command: str) -> bool:
    """True when `base_url` is serving; spawns `command` and waits when it is not.

    A remote URL is left alone (True: not silica's to start). Never raises — a
    server that fails to come up leaves the caller on the degraded path it would
    have taken anyway when nobody had started it.

    ponytail: one spawn, no retry. A command that dies on start (bad model path)
    shows up as the timeout warning and the log; re-running silica retries it.
    """
    if not base_url or not is_local(base_url):
        return True
    if _ready(base_url):
        return True
    from silica.ui.console import CONSOLE

    log_dir = Path.home() / ".silica" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{label}-server.log"
    logger.info("%s at %s is down — starting: %s", label, base_url, command)
    # Loading a model takes tens of seconds; say so rather than look hung.
    CONSOLE.print(f"  [dim]{label} at {base_url} is down — starting it…[/]")
    with open(log_path, "ab") as log:
        # start_new_session: the server is a daemon the next silica run should
        # find already up, so it must outlive this process and ignore its Ctrl-C.
        proc = subprocess.Popen(  # noqa: S602 — the command comes from the user's own .env
            command, shell=True, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    deadline = time.monotonic() + _READY_TIMEOUT
    while time.monotonic() < deadline:
        if _ready(base_url):
            logger.info("%s ready at %s", label, base_url)
            CONSOLE.print(f"  [dim]{label} ready.[/]")
            return True
        # A missing model file kills llama-server in under a second; without this
        # silica would poll a corpse for the whole timeout. Only a *failing* exit
        # counts: `lms server start` returns 0 immediately and leaves the daemon
        # loading behind it, so a clean exit still means "keep waiting".
        code = proc.poll()
        if code not in (None, 0):
            return _fail(label, f"server exited with code {code}", log_path)
        time.sleep(0.25)
    return _fail(
        label, f"did not come up within {_READY_TIMEOUT:.0f}s", log_path
    )


def ensure_local_servers(config=None) -> None:
    """Bring up every local endpoint whose start command the env carries.

    Best-effort and silent when nothing is configured: with no *_SERVE_CMD set
    this does not even probe, so startup cost for a hosted setup is zero.
    """
    from silica.config import CONFIG

    cfg = config or CONFIG
    for label, key, get_url in (
        ("chat", "SILICA_PROVIDER_SERVE_CMD", lambda: _chat_base_url(cfg)),
        ("embeddings", "SILICA_EMBEDDING_SERVE_CMD", lambda: cfg.embedding_base_url),
        ("rerank", "SILICA_RERANK_SERVE_CMD", lambda: cfg.rerank_base_url),
    ):
        command = os.getenv(key, "").strip()
        if command:
            ensure(label, get_url(), command)


def _chat_base_url(cfg) -> str:
    """Where the chat provider is served — the preset URL unless one is pinned."""
    if cfg.provider_base_url:
        return cfg.provider_base_url
    from silica.agent.providers import PROVIDER_PRESETS

    return PROVIDER_PRESETS.get(cfg.provider, {}).get("base_url", "")
