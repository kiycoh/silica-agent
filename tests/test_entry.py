# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The words launchers written before ADR-0130 still run never reach the REPL,
which would send their stdin to the chat model as a prompt."""
from __future__ import annotations

import subprocess
import sys

_RUN = ("import sys; from silica.entry import main; r = main(); "
        "assert 'silica.cli' not in sys.modules; sys.exit(r)")


def _silica(*args: str, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", _RUN, *args], input=stdin,
                          capture_output=True, text=True, timeout=60)


def test_retired_hooks_exit_zero_and_print_nothing():
    for args in (("hook", "SessionStart"), ("capture",)):
        r = _silica(*args, stdin='{"cwd": "/tmp", "hook_event_name": "SessionEnd"}')
        assert (r.returncode, r.stdout, r.stderr) == (0, "", ""), args


def test_retired_commands_fail_on_stderr_and_keep_stdout_clean():
    for word in ("mcp", "setup"):
        r = _silica(word, stdin='{"jsonrpc": "2.0", "method": "initialize"}')
        assert r.returncode == 1 and r.stdout == "", word
        assert f"`silica {word}` is gone" in r.stderr and "silica-core" in r.stderr
