# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""`silica setup <client>` — wire the MCP server into a coding agent's config.

The read side of the vault needs no model and no key, so serving it over MCP is
the shortest path from install to something useful. What stood between the two
was hand-pasting a JSON or TOML block into a file whose location the user has to
look up. This writes that block instead.

Never clobbers: an existing silica entry is left alone (the user may have tuned
it), the file is backed up before any write, and `--dry-run` prints what would
change. A file that does not parse is refused rather than overwritten.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
import tomllib
from pathlib import Path

from rich.markup import escape

from silica.config import CONFIG
from silica.ui.console import CONSOLE

# Everything this module prints carries a payload full of square brackets: the
# `[mcp]` extra, TOML table headers, a parser error quoting a `]`. rich reads a
# bracketed word as a style tag and drops it, which silently turned the printed
# install command into `--from silica-agent` — a command that runs and installs
# the wrong thing. Every interpolated value goes through escape(), and the
# config block (which has no styling of its own) prints with markup off.

# What every client is told to run. uvx keeps the server at one command with no
# install step of its own, which is the whole point of the generated block.
MCP_COMMAND = ["uvx", "--from", "silica-agent[mcp]", "silica", "mcp"]

CLIENTS = ("claude", "codex", "opencode")


def _default_path(client: str) -> Path:
    home = Path.home()
    if client == "codex":
        return home / ".codex" / "config.toml"
    return home / ".config" / "opencode" / "opencode.json"


def _vault() -> str:
    """Absolute vault path for the generated env block, or "" to omit it.

    An MCP client starts the server from its own working directory, so a
    relative path (or none at all, which resolves to ~/.silica/vault) would
    point somewhere the user did not mean.
    """
    raw = CONFIG.vault_path.strip()
    return str(Path(raw).expanduser().resolve()) if raw else ""


def _backup(path: Path) -> Path:
    """Timestamped copy beside the original, returned for the report."""
    dest = path.with_suffix(path.suffix + f".bak.{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, dest)
    return dest


def _report(path: Path, block: str, dry_run: bool, backup: Path | None) -> int:
    if dry_run:
        CONSOLE.print(f"  [dim]would write to {escape(str(path))}:[/]")
        CONSOLE.print(block, markup=False)
        return 0
    CONSOLE.print(f"  [green]✓[/] wrote {escape(str(path))}")
    if backup:
        CONSOLE.print(f"  [dim]backup: {escape(str(backup))}[/]")
    return 0


def _codex_block() -> str:
    vault = _vault()
    args = ", ".join(f'"{a}"' for a in MCP_COMMAND[1:])
    block = (
        "\n[mcp_servers.silica]\n"
        f'command = "{MCP_COMMAND[0]}"\n'
        f"args = [{args}]\n"
    )
    if vault:
        block += f'\n[mcp_servers.silica.env]\nSILICA_VAULT = "{vault}"\n'
    return block


def _setup_codex(path: Path, dry_run: bool) -> int:
    """Append the server block to ~/.codex/config.toml.

    ponytail: appended as text, not re-serialised. tomllib reads but cannot
    write, and a real TOML writer is a dependency for one block — appending
    also preserves the comments and ordering a round-trip would flatten. The
    ceiling is that it only ever adds a top-level table; if silica ever needs
    to edit an existing entry, that is when tomlkit earns its place.
    """
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if existing.strip():
        try:
            parsed = tomllib.loads(existing)
        except tomllib.TOMLDecodeError as e:
            CONSOLE.print(f"  [red]✗[/] {escape(str(path))} is not valid TOML ({escape(str(e))}) — not touching it")
            return 1
        if "silica" in parsed.get("mcp_servers", {}):
            CONSOLE.print(f"  [dim]silica is already configured in {escape(str(path))} — nothing to do[/]")
            return 0
    block = _codex_block()
    if dry_run:
        return _report(path, block, True, None)
    backup = _backup(path) if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    sep = "" if not existing or existing.endswith("\n") else "\n"
    path.write_text(existing + sep + block, encoding="utf-8")
    return _report(path, block, False, backup)


def _setup_opencode(path: Path, dry_run: bool) -> int:
    """Merge the server into opencode.json under `mcp.silica`."""
    data: dict = {}
    if path.exists() and path.read_text(encoding="utf-8").strip():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            CONSOLE.print(f"  [red]✗[/] {escape(str(path))} is not valid JSON ({escape(str(e))}) — not touching it")
            return 1
        if not isinstance(data, dict):
            CONSOLE.print(f"  [red]✗[/] {escape(str(path))} is not a JSON object — not touching it")
            return 1
        if "silica" in data.get("mcp", {}):
            CONSOLE.print(f"  [dim]silica is already configured in {escape(str(path))} — nothing to do[/]")
            return 0
    entry: dict = {"type": "local", "command": MCP_COMMAND, "enabled": True}
    vault = _vault()
    if vault:
        entry["environment"] = {"SILICA_VAULT": vault}
    data.setdefault("mcp", {})["silica"] = entry
    block = json.dumps(data, indent=2) + "\n"
    if dry_run:
        return _report(path, block, True, None)
    backup = _backup(path) if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(block, encoding="utf-8")
    return _report(path, block, False, backup)


def _setup_claude(dry_run: bool) -> int:
    """Delegate to `claude mcp add`.

    Claude Code owns its own config format and ships a command for exactly this,
    so writing the file by hand would be a second implementation to keep in sync
    with theirs. When the CLI is absent, printing the command is still the whole
    answer.
    """
    vault = _vault()
    cmd = ["claude", "mcp", "add", "--transport", "stdio", "silica"]
    if vault:
        cmd += ["--env", f"SILICA_VAULT={vault}"]
    cmd += ["--", *MCP_COMMAND]
    printable = " ".join(cmd)
    if dry_run or not shutil.which("claude"):
        if not dry_run:
            CONSOLE.print("  [yellow]⚠[/] the `claude` CLI is not on PATH — run this yourself:")
        # soft_wrap so rich does not fold the line at the console width: this is
        # a command meant to be copied, and a wrap puts a real newline in the
        # middle of it, so pasting runs a fragment.
        CONSOLE.print(f"  {printable}", markup=False, soft_wrap=True)
        return 0
    result = subprocess.run(cmd)
    if result.returncode != 0:
        CONSOLE.print(f"  [red]✗[/] `{escape(printable)}` failed")
        return result.returncode
    CONSOLE.print("  [green]✓[/] registered with Claude Code")
    CONSOLE.print(
        "  [dim]for the recall/capture skill too: "
        "claude plugin marketplace add kiycoh/silica-agent && "
        "claude plugin install silica@silica[/]"
    )
    return 0


def run_setup(args: list[str]) -> int:
    """`silica setup <client> [--dry-run] [--config PATH]`."""
    positional = [a for a in args if not a.startswith("-")]
    client = positional[0] if positional else ""
    if client not in CLIENTS:
        CONSOLE.print(f"  Usage: silica setup <{'|'.join(CLIENTS)}> [--dry-run] [--config PATH]", markup=False)
        return 1
    dry_run = "--dry-run" in args
    if client == "claude":
        return _setup_claude(dry_run)
    override = next((a.split("=", 1)[1] for a in args if a.startswith("--config=")), "")
    if not override and "--config" in args:
        i = args.index("--config")
        override = args[i + 1] if i + 1 < len(args) else ""
    path = Path(override).expanduser() if override else _default_path(client)
    if client == "codex":
        return _setup_codex(path, dry_run)
    return _setup_opencode(path, dry_run)
