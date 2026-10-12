# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""/connect: the MCP client's Settings block, from the REPL (ADR-0091).

Every verb is one of the block's actions, so a row reads the same here and in
Settings › Connectors. A pasted `add` line can hold an inline key, so `add`
and `secure` lines are never recorded in the prompt history (the
`secret_verbs` of the /connect row, silica/ui/commands.py). A missing secret
is never asked on the line: it is asked in a separate hidden prompt with no
history (ADR-0089 point 7), and without a terminal it is not asked at all.

Out of cli.py for the reason zotero_cli.py is: cli.py's string literals that
start with a slash are read as slash commands by the registry parity test.
"""
from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from silica.connectors.mcp_block import McpBlock

USAGE = "usage: /connect [list|add <json>|secure <key>|login <key>|logout <key>|remove <key>|reload]"


def _hidden(prompt: str) -> str:
    from prompt_toolkit import prompt as ask

    return ask(prompt, is_password=True)


def _yes(prompt: str) -> bool:
    from prompt_toolkit import prompt as ask

    return ask(prompt).strip().lower() in ("y", "yes")


def _block() -> McpBlock:
    # The block itself, not `find("mcp")`: find is Optional for a name outside
    # the static tuple, and "mcp" is always in it.
    from silica.connectors.mcp_block import MCP_BLOCK

    return MCP_BLOCK


def _list(console) -> None:
    st = _block().status()
    if not st["rows"]:
        console.print(f"  {st['detail']} ({st['file']})")
        return
    console.print(f"  {st['detail']} · {st['file']}")
    for r in st["rows"]:
        console.print(f"  [bold]{r['key']}[/]  [dim]{r['kind']}[/]  {r['state']}", highlight=False)


def _secure(key: str, console, ask_secret: Callable[[str], str] | None,
            ask_yes: Callable[[str], bool] | None) -> None:
    from silica.connectors import credentials, mcp_config

    block = _block()
    out = block.act("secure", {"key": key})
    if not out["ok"]:
        console.print(f"  [yellow]{out['error']}[/]")
        return
    for label in out.get("moved") or ():
        console.print(f"  moved {label} to the {credentials.where(mcp_config.scope(key))}")
    entry = mcp_config.load().get(key)
    needs = entry.needs if entry is not None else []
    if not needs:
        if not out.get("moved"):
            console.print(f"  {key}: no inline secret and nothing missing")
        return
    if ask_secret is None:
        console.print(f"  {key} needs {', '.join(needs)}: no terminal to ask in; "
                      "set it in Settings › Connectors")
        return
    if not credentials.where(mcp_config.scope(key)):
        path = credentials.file_path(mcp_config.scope(key))
        console.print(f"  no keyring on this machine: a secret can be kept in {path} (0600)")
        if ask_yes is None or not ask_yes("  keep it in that file? [y/N] "):
            console.print("  nothing saved")
            return
        block.act("store_in_file", {"key": key})
    for name in needs:
        value = ask_secret(f"{name} for {key}: ")
        if not value:
            console.print(f"  {name}: nothing typed, nothing saved")
            continue
        res = block.act("secret", {"key": key, "name": name, "value": value})
        if res["ok"]:
            where = "stored in a file" if res["stored"] == "file" else "stored in the keyring"
            console.print(f"  {name}: {where}")
        else:
            console.print(f"  [yellow]{name}: {res['error']}[/]")


def _login(key: str, console, poll_s: float, ask_line: Callable[[str], str] | None,
           ask_yes: Callable[[str], bool] | None) -> None:
    from silica.connectors import mcp_oauth
    from silica.connectors.mcp_client import CallFailed

    try:
        out = mcp_oauth.login(key)
    except CallFailed as exc:
        console.print(f"  [yellow]{exc}[/]")
        if str(exc).startswith("needs a credentials store"):
            if ask_yes is None or not ask_yes("  keep this login in a 0600 file under ~/.silica/credentials? [y/N] "):
                return
            _block().act("store_in_file", {"key": key})
        elif str(exc).startswith("needs a client ID") and ask_line is not None:
            # The spec's last registration step: a client ID registered by hand.
            client_id = ask_line(f"  client ID registered by hand for {key} (empty to skip): ").strip()
            if not client_id:
                return
            _block().act("client_id", {"key": key, "client_id": client_id})
        else:
            return
        try:
            out = mcp_oauth.login(key)
        except Exception as again:  # the reason is the message
            console.print(f"  [yellow]{again}[/]")
            return
    except Exception as exc:  # ConfigError: the reason is the message
        console.print(f"  [yellow]{exc}[/]")
        return
    if out.get("connected"):
        console.print(f"  {key}: connected")
        return
    if out.get("url"):
        # Printed as well as opened: a terminal without a browser (SSH) copies it.
        console.print(f"  open this page to log in to {key}:\n  {out['url']}", highlight=False, soft_wrap=True)
    console.print("  waiting for the browser (Ctrl+C stops waiting; the login goes on)")
    deadline = time.monotonic() + mcp_oauth.LOGIN_TIMEOUT_S + 30
    try:
        while time.monotonic() < deadline:
            state = mcp_oauth.login_state(key)
            if state.get("done"):
                if state.get("error"):
                    console.print(f"  [yellow]{key}: {state['error']}[/]")
                else:
                    console.print(f"  {key}: connected")
                return
            time.sleep(poll_s)
    except KeyboardInterrupt:
        console.print("  stopped waiting; /connect shows the row's state")
        return
    except CallFailed as exc:
        console.print(f"  [yellow]{exc}[/]")


def _line(prompt: str) -> str:
    from prompt_toolkit import prompt as ask

    return ask(prompt)


def run(line: str, console, *, ask_secret: Callable[[str], str] | None = None,
        ask_yes: Callable[[str], bool] | None = None, ask_line: Callable[[str], str] | None = None,
        poll_s: float = 1.0) -> None:
    """One /connect line. `ask_secret`/`ask_yes` default to hidden prompts
    when stdin is a terminal, and to None (never ask) when it is not: the GUI
    runs direct commands under a captured console with no one to type."""
    from silica.connectors import credentials, mcp_config

    if mcp_config.hosted():
        console.print("  connectors are local only")
        return
    if sys.stdin.isatty():
        ask_secret = ask_secret or _hidden
        ask_yes = ask_yes or _yes
        ask_line = ask_line or _line
    _, _, rest = line.strip().partition(" ")
    verb, _, arg = rest.strip().partition(" ")
    verb, arg = verb.lower() or "list", arg.strip()
    block = _block()
    if verb == "list":
        _list(console)
    elif verb == "add":
        out = block.act("add", {"json": arg})
        if not out["ok"] and out.get("no_store"):
            keys = ", ".join(mcp_config.paste_keys(arg))
            console.print(f"  no keyring on this machine: the secrets of {keys} can be kept in "
                          f"{credentials.folder()} (one 0600 file per server)")
            if ask_yes is not None and ask_yes("  keep them there? [y/N] "):
                out = block.act("add", {"json": arg, "store_in_file": True})
            else:
                console.print("  nothing added")
                return
        console.print(f"  added {', '.join(out['added'])}" if out["ok"] else f"  [yellow]{out['error']}[/]")
        for why in out.get("refused") or ():
            console.print(f"  [yellow]not added: {why}[/]", highlight=False)
        if out["ok"]:
            _list(console)
    elif verb == "secure" and arg:
        _secure(arg, console, ask_secret, ask_yes)
    elif verb == "login" and arg:
        _login(arg, console, poll_s, ask_line, ask_yes)
    elif verb in ("logout", "remove") and arg:
        out = block.act(verb, {"key": arg})
        done = "logged out of" if verb == "logout" else "removed"
        console.print(f"  {done} {arg}" if out["ok"] else f"  [yellow]{out['error']}[/]")
    elif verb == "reload":
        out = block.act("reload", {})
        if out["ok"]:
            tools = ", ".join(out["tools"]) or "no connector"
            console.print(f"  reloaded: {tools} (the chat's tool list changes once)")
        else:
            console.print(f"  [yellow]{out['error']}[/]")
    else:
        console.print(f"  {USAGE}")
