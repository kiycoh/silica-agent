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

A serve command is a command silica SPAWNS, so where it comes from is a trust
boundary. It is held at the source rather than here: config.py layers
~/.silica/.env and nothing else, so the .env of whatever checkout the shell
happens to sit in never reaches os.environ to begin with.
"""
from __future__ import annotations

import logging
import os
import re
import struct
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from silica.kernel.scrub import scrub_credentials

logger = logging.getLogger(__name__)

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
_READY_TIMEOUT = float(os.getenv("SILICA_SERVE_TIMEOUT", "180"))


def is_local(base_url: str) -> bool:
    """Whether this endpoint is one silica could start — a remote one is not ours."""
    url = urlparse(base_url if "//" in base_url else f"//{base_url}")
    return url.hostname in _LOCAL_HOSTS


def ready(base_url: str) -> bool:
    """True when the endpoint answers as a *loaded* server. Public: the web GUI
    asks it the same question before offering dictation.

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
    "stt": "the web GUI's dictation button stays off",
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
        # scrub: a server's own log commonly restates its launch argv, key and all.
        CONSOLE.print(f"    [dim]{scrub_credentials(line)[:200]}[/]")
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
    if ready(base_url):
        return True
    from silica.ui.console import CONSOLE

    log_dir = Path.home() / ".silica" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{label}-server.log"
    # Never echo the URL or the command as configured: a proxy endpoint carries
    # its key in the userinfo or the query, a serve command as a flag, and this
    # line precedes the doctor's own report.
    shown = scrub_credentials(base_url)
    logger.info("%s at %s is down — starting: %s", label, shown, scrub_credentials(command))
    # Loading a model takes tens of seconds; say so rather than look hung.
    CONSOLE.print(f"  [dim]{label} at {shown} is down — starting it…[/]")
    with open(log_path, "ab") as log:
        # start_new_session: the server is a daemon the next silica run should
        # find already up, so it must outlive this process and ignore its Ctrl-C.
        # shell=True is the feature (a script path, `lms server start`, a chained
        # command), and it is only defensible because the only .env silica
        # reads is the user's own — see the layering note in config.py.
        proc = subprocess.Popen(  # noqa: S602 — trusted source only, see above
            command, shell=True, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    deadline = time.monotonic() + _READY_TIMEOUT
    while time.monotonic() < deadline:
        if ready(base_url):
            logger.info("%s ready at %s", label, shown)
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


# llama-server keeps up to 8 GiB of prompt states in host RAM by default
# (--cache-ram 8192), and --cache-idle-slots, on by default, saves every idle
# slot there on each new task of ANY kind, so an embedding server fills it too
# (llama.cpp tools/server/server-context.cpp, 2026-07-17 and 448147d42).
# Measured 2026-10-08: a jina embedding server re-embedding the vault was
# killed at a 4 GB memory cap after 20 s; with --cache-ram 0 the same job
# peaked at 2.7 GB. Only the chat server gains from the cache: a conversation
# that a side call pushed out of the slot comes back without a 64k prefill. So
# the chat server gets two full-context entries, at most 12% of the machine's
# RAM, and every other llama-server none. An explicit --cache-ram always wins.
_CACHE_RAM_SHARE = 0.12
_GGUF_SCALARS = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}


def _gguf_metadata(path: str) -> dict:
    """The key/value header of a GGUF file (format v2/v3), tensors not read."""
    with open(path, "rb") as f:
        def read(fmt):
            return struct.unpack("<" + fmt, f.read(struct.calcsize("<" + fmt)))[0]

        def value(kind):
            if kind == 8:
                return f.read(read("Q")).decode("utf-8", "replace")
            if kind == 9:
                item, n = read("I"), read("Q")
                return [value(item) for _ in range(n)]
            return read(_GGUF_SCALARS[kind])

        if f.read(4) != b"GGUF" or read("I") < 2:
            raise ValueError(f"{path} is not a GGUF v2+ file")
        read("Q")  # tensor count
        meta = {}
        for _ in range(read("Q")):
            key = f.read(read("Q")).decode("utf-8", "replace")
            meta[key] = value(read("I"))
        return meta


def prompt_cache_entry_bytes(meta: dict, n_ctx: int) -> int:
    """Host RAM one full-context prompt-cache entry takes: the f16 KV of the
    attention layers, plus, on a hybrid model, its recurrent state once and once
    per context checkpoint (llama-server keeps one every 8192 tokens, at most 32).

    ponytail: a sliding-window model is counted as full attention, so its entry
    reads high; the 12% RAM ceiling bounds the result either way.
    """
    arch = meta["general.architecture"]

    def get(key: str, default: Any = 0) -> Any:
        return meta.get(f"{arch}.{key}", default)

    n_layer = get("block_count")
    interval = get("full_attention_interval", 1)
    n_attn = n_layer // interval if interval else n_layer
    n_kv = get("attention.head_count_kv", get("attention.head_count"))
    n_kv = max(n_kv) if isinstance(n_kv, list) else n_kv
    head = get("embedding_length") // max(1, get("attention.head_count", 1))
    kv = n_attn * n_kv * (get("attention.key_length", head) + get("attention.value_length", head)) * 2
    inner, state = get("ssm.inner_size"), get("ssm.state_size")
    conv = (get("ssm.conv_kernel", 1) - 1) * (inner + 2 * get("ssm.group_count") * state)
    recurrent = (n_layer - n_attn) * (inner * state + conv) * 4  # f32
    checkpoints = min(32, n_ctx // 8192) if recurrent else 0
    return n_ctx * kv + recurrent * (1 + checkpoints)


def with_cache_ram(label: str, command: str) -> str:
    """`command` with a --cache-ram cap when it runs llama-server without one."""
    import shlex

    try:
        argv = shlex.split(command)
    except ValueError:
        return command
    if not argv or Path(argv[0]).name != "llama-server" or {"--cache-ram", "-cram"} & set(argv):
        return command
    mib = 0
    if label == "chat":
        opt = lambda *names: next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a in names), None)  # noqa: E731
        try:
            ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") * _CACHE_RAM_SHARE
        except (AttributeError, ValueError, OSError):
            ram = float("inf")  # Windows: no sysconf; the GGUF term alone bounds it
        entry: float
        try:
            model, ctx = opt("-m", "--model"), opt("-c", "--ctx-size")
            if model is None or ctx is None:
                raise ValueError("no -m or no -c")
            entry = prompt_cache_entry_bytes(_gguf_metadata(model), int(ctx))
        except (OSError, ValueError, KeyError, TypeError, struct.error):
            # No -m, no -c, or a file this reader cannot parse: llama-server would
            # size the context from the model itself, which this cannot see, so
            # the RAM share is the only bound left.
            entry = float("inf")
        cap = min(2 * entry, ram)
        if cap == float("inf"):
            return command
        mib = int(cap // 2**20)
    return f"{command} --cache-ram {mib}"



def with_mmproj(label: str, command: str) -> str:
    """The chat llama-server with the vision projector that sits beside its model.

    Qwen3.5 is one model for text and images, and llama-server takes images
    only with `--mmproj` (ADR-0077). Found, not configured: a model repo ships
    its own projector next to the GGUF, and a projector fits only the model it
    was cut for (the 9B's is 4096 wide, the 4B's hidden size is 2560), so the
    file beside `-m` is the one that fits, and a guess from elsewhere is not.
    """
    import shlex

    try:
        argv = shlex.split(command)
    except ValueError:
        return command
    if label != "chat" or not argv or Path(argv[0]).name != "llama-server" or {"--mmproj", "-mm"} & set(argv):
        return command
    model = next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a in ("-m", "--model")), None)
    if not model:
        return command
    found = sorted(Path(model).parent.glob("mmproj*.gguf"))
    return f"{command} --mmproj {shlex.quote(str(found[0]))}" if found else command


def ensure_local_servers(config=None) -> None:
    """Bring up every local endpoint whose start command the env carries.

    Best-effort and silent when nothing is configured: with no *_SERVE_CMD set
    this does not even probe, so startup cost for a hosted setup is zero.
    """
    from silica.config import CONFIG

    cfg = config or CONFIG
    for label, key, get_url in (
        ("chat", "SILICA_PROVIDER_SERVE_CMD", lambda: chat_base_url(cfg)),
        ("embeddings", "SILICA_EMBEDDING_SERVE_CMD", lambda: cfg.embedding_base_url),
        ("rerank", "SILICA_RERANK_SERVE_CMD", lambda: cfg.rerank_base_url),
        ("stt", "SILICA_STT_SERVE_CMD", lambda: cfg.stt_base_url),
    ):
        command = os.getenv(key, "").strip()
        if command:
            ensure(label, get_url(), with_cache_ram(label, with_mmproj(label, command)))


def chat_base_url(cfg) -> str:
    """Where the chat provider is served — the preset URL unless one is pinned."""
    if cfg.provider_base_url:
        return cfg.provider_base_url
    from silica.agent.providers import PROVIDER_PRESETS

    return PROVIDER_PRESETS.get(cfg.provider, {}).get("base_url", "")
