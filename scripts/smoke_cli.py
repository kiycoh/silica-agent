#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Automated slice of docs/manual-cli-tests.md.

Runs every CLI case that is deterministic and side-effect free against a scratch
copy of the synthetic vault, so the manual pass is left with only what needs a
human: the ingest lane, the web lane, undo/revert, the GUI and the Obsidian
bridge.

    uv run python scripts/smoke_cli.py            # everything
    uv run python scripts/smoke_cli.py --offline  # skip the groups needing a live endpoint

Exit 0 when every case passed. Nothing here touches the real config.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "synthetic_vault"
SILICA = [sys.executable, "-m", "silica.cli"]
FAREWELL = "(_  _)"  # printed on /exit: proof the REPL survived the whole batch

results: list[tuple[bool, str, str]] = []  # (ok, name, detail)


def check(ok: bool, name: str, detail: str = "") -> bool:
    results.append((bool(ok), name, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail and not ok else ''}")
    return bool(ok)


def run(args: list[str], cwd: Path, stdin: str = "", timeout: int = 180):
    """A silica invocation. stdin='' means closed, which is what the shell
    subcommands want and what makes `mcp` return instead of waiting."""
    return subprocess.run(
        SILICA + args, cwd=cwd, input=stdin, capture_output=True,
        text=True, timeout=timeout, env={**os.environ, "SILICA_VAULT": str(cwd)},
    )


def md_digest(vault: Path) -> str:
    """Fingerprint of every note in the vault. Only .md: the index and cache
    writes are legitimate, a changed note under a read-only command is not."""
    h = hashlib.sha256()
    for p in sorted(vault.rglob("*.md")):
        h.update(str(p.relative_to(vault)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()


# --------------------------------------------------------------------------
# Phase A — shell subcommands
# --------------------------------------------------------------------------

def phase_a(vault: Path) -> None:
    print("\nPhase A — shell subcommands")

    r = run(["update", "--check"], vault)
    check(r.returncode == 0 and "Traceback" not in r.stderr,
          "update --check reports without changing anything", r.stderr[-200:])

    r = run(["doctor"], vault, timeout=300)
    check("Traceback" not in r.stderr, "doctor runs to a report", r.stderr[-300:])
    print(f"        (doctor exit {r.returncode} — non-zero is a real finding, read the table)")


# --------------------------------------------------------------------------
# Phase B — REPL command batches
# --------------------------------------------------------------------------

# (name, commands, writes_notes, needs_endpoint)
GROUPS: list[tuple[str, list[str], bool, bool]] = [
    ("system + read-only", [
        "/vault", "/model", "/tools", "/help", "/settings",
        "/status", "/plans", "/contested", "/stale", "/stale --all",
        "/impact", "/review",
        "/thinking", "/thinking",
        "/verbose", "/verbose", "/verbose", "/verbose",
        "/clear",
    ], False, False),
    ("indices", ["/cooccur", "/lexical", "/embed"], False, True),
    ("search", [
        "/find gradient --k=3",
        "/path Concepts/Gradient.md Concepts/Perceptron.md",
    ], False, True),
    ("views", ["/graph graph.html", "/map Concepts/Gradient.md"], False, True),
    ("curate dry-run", ["/curate"], False, True),
]


def batch(vault: Path, commands: list[str], timeout: int) -> subprocess.CompletedProcess:
    return run([], vault, stdin="".join(c + "\n" for c in commands) + "/exit\n", timeout=timeout)


def phase_b(vault: Path, offline: bool) -> None:
    print("\nPhase B — REPL commands")

    r = batch(vault, [], timeout=120)
    if not check(FAREWELL in r.stdout, "REPL reaches the prompt and exits",
                 "no farewell: an unconfigured install autolaunches the wizard, "
                 "which eats the piped stdin. Run `silica init` first."):
        return

    for name, commands, writes, needs_endpoint in GROUPS:
        if offline and needs_endpoint:
            print(f"  SKIP  {name} (--offline)")
            continue
        before = md_digest(vault)
        r = batch(vault, commands, timeout=600)
        # ponytail: crash-only oracle. A command that prints a polite error and
        # keeps going still passes. Add an expected-substring per command if a
        # silent no-op ever ships.
        ok = r.returncode == 0 and FAREWELL in r.stdout and "Traceback" not in r.stdout
        tail = (r.stdout + r.stderr)[-400:]
        check(ok, f"{name}: {len(commands)} commands, no crash", tail)
        if not writes:
            check(md_digest(vault) == before, f"{name}: wrote no note")

    if not offline:
        check((vault / "graph.html").exists(), "graph export produced a file")


# --------------------------------------------------------------------------

def main() -> int:
    offline = "--offline" in sys.argv
    with tempfile.TemporaryDirectory(prefix="silica-smoke-") as td:
        tmp = Path(td)
        vault = tmp / "vault"
        shutil.copytree(FIXTURE, vault)
        print(f"scratch vault: {vault}")
        phase_a(vault)
        phase_b(vault, offline)

    failed = [name for ok, name, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    for name in failed:
        print(f"  failed: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
