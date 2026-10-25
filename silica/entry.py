# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""`silica` console-script entry.

It once dispatched the coding-agent harness hooks (`hook`, `capture`) before
`silica.cli` was imported, because that import cost 3.3 s inside someone
else's session. The hooks left with the plugin (ADR-0130); the module stays
because the console script and the `silica-harness` shim under packaging/
both name `silica.entry:main`.
"""
from __future__ import annotations

import sys

# Launchers written before ADR-0130 outlive it: a Claude Code plugin keeps its
# cached hooks.json (`silica hook|capture` at every session start, end and
# compaction), and every entry `silica setup` registered runs `silica mcp`,
# all through uvx from PyPI. Unknown to the CLI, each word fell through to the
# REPL, which started the model servers and sent the hook's JSON, or the MCP
# client's JSON-RPC, to the chat model as a prompt (seen 2026-10-24). So the
# hooks stay words that do nothing and exit 0, the contract they had, and the
# two commands say they are gone on stderr (stdout is an MCP client's protocol
# channel) and exit 1, which the client shows as a failed server.
# ponytail: kill by 2027-04-24, six months of releases for those entries to be
# removed; the upgrade is a CLI that refuses an unknown subcommand instead of
# opening the REPL on it.
_RETIRED_HOOKS = ("hook", "capture")
_RETIRED_COMMANDS = ("mcp", "setup")


def main() -> int:
    word = sys.argv[1] if len(sys.argv) > 1 else ""
    if word in _RETIRED_HOOKS:
        return 0
    if word in _RETIRED_COMMANDS:
        print(f"`silica {word}` is gone: silica-agent no longer serves MCP. Remove its "
              "server entry from your client (Claude Code: `claude mcp remove silica`); "
              "coding agents use silica-core.", file=sys.stderr)
        return 1
    from silica.cli import main as cli_main
    return cli_main()
