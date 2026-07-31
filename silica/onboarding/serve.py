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
        subprocess.Popen(  # noqa: S602 — the command comes from the user's own .env
            command, shell=True, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    deadline = time.monotonic() + _READY_TIMEOUT
    while time.monotonic() < deadline:
        if _ready(base_url):
            logger.info("%s ready at %s", label, base_url)
            CONSOLE.print(f"  [dim]{label} ready.[/]")
            return True
        time.sleep(0.25)
    logger.warning(
        "%s did not come up within %.0fs — see %s", label, _READY_TIMEOUT, log_path
    )
    CONSOLE.print(
        f"  [yellow]{label} did not come up within {_READY_TIMEOUT:.0f}s[/] "
        f"[dim]— see {log_path}[/]"
    )
    return False


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
