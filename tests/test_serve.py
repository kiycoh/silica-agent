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


def test_dead_server_fails_fast_and_loud(tmp_path, monkeypatch, capsys):
    """A bad model path kills llama-server at once — don't poll a corpse for 180s,
    and don't let the user find out later from quietly worse answers."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(serve, "_READY_TIMEOUT", 30.0)
    started = time.monotonic()

    cmd = "echo 'failed to open GGUF file' >&2; exit 1"
    assert not serve.ensure("embeddings", f"http://127.0.0.1:{_free_port()}/v1", cmd)

    assert time.monotonic() - started < 5
    out = capsys.readouterr().out
    assert "exited with code 1" in out
    assert "failed to open GGUF file" in out  # the cause, not just a log path
    assert "co-occurrence" in out  # what it costs the user


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


def test_a_serve_command_in_the_environment_is_spawned(tmp_path, monkeypatch):
    """The other half of test_unset_command_never_probes: set means spawn.

    Where the command may come from is settled in config.py, which layers only
    ~/.silica/.env — see test_dotenv_layering. By the time it reaches os.environ
    it is the user's own, so this reads it directly.
    """
    import subprocess

    monkeypatch.setenv("HOME", str(tmp_path))
    for key in ("SILICA_RERANK_SERVE_CMD", "SILICA_PROVIDER_SERVE_CMD",
                "SILICA_STT_SERVE_CMD"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SILICA_EMBEDDING_SERVE_CMD", "lms server start")
    monkeypatch.setattr(serve, "ready", lambda url: False)
    spawned: list[str] = []

    class FakeProc:
        def poll(self):
            return 1  # a failing exit ends ensure() at once, no 180s wait

    monkeypatch.setattr(
        subprocess, "Popen",
        lambda command, **kw: (spawned.append(command), FakeProc())[1],
    )
    serve.ensure_local_servers()

    assert spawned == ["lms server start"]
