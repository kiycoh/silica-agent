# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Endpoint autostart: probe, spawn, wait for the model to load (onboarding/serve.py)."""
import shlex
import socket
import sys
import time

from silica.onboarding import serve


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _loading_server_cmd(port: int, load_seconds: float) -> str:
    """A server that binds at once but answers 503 while it "loads", like llama.cpp.

    Self-terminates so a failing test cannot leave a process behind.
    """
    code = (
        "import http.server, threading, time, os\n"
        f"ready_at = time.monotonic() + {load_seconds}\n"
        "threading.Timer(30, lambda: os._exit(0)).start()\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200 if time.monotonic() > ready_at else 503)\n"
        "        self.end_headers()\n"
        "    def log_message(self, *a): pass\n"
        f"http.server.HTTPServer(('127.0.0.1', {port}), H).serve_forever()\n"
    )
    return f"{sys.executable} -c {shlex.quote(code)}"


def test_waits_for_load_not_just_for_the_port(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # logs under a throwaway ~/.silica
    port = _free_port()
    url = f"http://127.0.0.1:{port}/v1"
    started = time.monotonic()

    assert serve.ensure("test", url, _loading_server_cmd(port, load_seconds=1.5))

    # The port is open within milliseconds; returning before ~1.5s would mean
    # silica handed a still-loading server to the first real request.
    assert time.monotonic() - started >= 1.5
    assert (tmp_path / ".silica" / "logs" / "test-server.log").exists()
    # Already up now: `false` would fail if the second call spawned anything.
    assert serve.ensure("test", url, "false")


def test_remote_url_is_left_alone():
    assert serve.ensure("chat", "https://api.openai.com/v1", "false")
    assert not serve.is_local("https://api.openai.com/v1")
    assert serve.is_local("http://localhost:1234/v1")


def test_unset_command_never_probes(monkeypatch):
    for key in ("SILICA_EMBEDDING_SERVE_CMD", "SILICA_RERANK_SERVE_CMD",
                "SILICA_PROVIDER_SERVE_CMD"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(serve, "ensure", lambda *a: (_ for _ in ()).throw(AssertionError))
    serve.ensure_local_servers()
