# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The one door from a surface to the slash commands.

`dispatch` walks the rows of `silica.command_table` and hands back a `Result` for the
surface to render. A `/word` with no row is tried as a skill (ADR-0063). Asking the table
the REPL asks is the point: the hand-kept list of "web commands" that used to gate
the browser drifted, and /lexical /wiki /graph /map /find /vault fell through it
into an error.

The two surfaces call it at a different point, and that is what each of them does
today, kept and named here rather than left to read as the same one. The REPL asks
this door, then `/web`, then its own session commands, and hands an unknown
command to the model. `run_turn` asks the withheld list, `/web`, then this door,
and refuses an unknown command. Only a skill named `web` tells the two orders
apart: the REPL runs it, the browser the web turn.
"""
from __future__ import annotations

import importlib
import shlex
from collections.abc import Callable
from contextlib import AbstractContextManager

from silica.command_table import ROWS, Ask, Call, Command, Result, unseen
from silica.config import CONFIG
from silica.session.words import command_word, direct_args

# How a read of a note fails: `NoteNotFound` and a refusal of a path outside the
# vault are RuntimeErrors, a dropped bridge an OSError, a file that is no text a
# UnicodeDecodeError. A handler says these and goes on; anything else is a fault.
UNREADABLE = (RuntimeError, OSError, ValueError)


def _run(row: Command, call: Call) -> Result:
    """The row's handler, imported now: a process pays for what a line asks for."""
    module, name = row.handler.split(":")
    return getattr(importlib.import_module(module), name)(call)


def dispatch(text: str, messages: list[dict], *, ask: Ask | None = None,
             progress: Callable[[Result], AbstractContextManager] = unseen,
             live: Callable[..., None] | None = None) -> Result | None:
    """Run the command `text` names. None: no command by that name, nothing ran.

    `ask` is the surface's way to put a question to the person at it, None when
    there is nobody to ask. `progress` opens a surface's renderer for a command that
    runs an agent loop of its own. `live` prints a line the moment it is said, for
    the surface that shows work as it happens.
    """
    from silica.tools import catalog

    catalog.load()
    out = Result(live=live)
    words = direct_args(text.strip())
    row = ROWS.get(words[0].lower()) if words else None
    if row is not None and row.handler and not row.shlex:
        return _run(row, Call(words[1:], text, messages, out, ask, progress))
    if not text.strip().startswith("/"):
        return None  # plain prose can have stray quotes and apostrophes: no shlex
    row = ROWS.get(command_word(text))
    if row is None or not row.shlex:
        return expand_skill(text, out)
    try:
        args = shlex.split(text.strip())[1:]  # honours quoted paths with spaces
    except ValueError:
        return out.refused("unbalanced quotes in command. Wrap paths with spaces in \"...\".")
    return _run(row, Call(args, text, messages, out, ask, progress))


def expand_skill(text: str, out: Result) -> Result | None:
    """`/<name> <args>` through the skill of that name, None when there is none.

    The arguments go over as typed, never through shlex here: the skill's own
    placeholders decide how they are read, and an apostrophe in prose
    (/explain dell'entropia) must not be a quoting error.
    """
    from silica_core.kernel import skills

    cmd, *rest = text.strip().split(None, 1)
    found = skills.discover(CONFIG.vault_path or None)
    skill = found.get(cmd[1:].lower())
    if skill is None:
        return None
    args = rest[0] if rest else ""
    try:
        body = skills.expand(skill, args)
    except ValueError as e:  # the usage line: shown, and the model is never asked
        return out.refused(f"{e}")
    if skill.name == "diagram" and skill.source == "builtin" and (CONFIG.model or "").startswith("custom/"):
        # ADR-0082 gate 2: Qwen3.5-9B's /diagram parsed 46 of 48 once quoted,
        # but 5 of 10 audited carried arrows its note does not hold (a list
        # chained into a sequence that never happens), and a wrong diagram
        # teaches worse than none. A local model answers with the table
        # /schematize writes. ponytail: keyed on the route, so a large model
        # served locally gets the table too; rerun evals/probe_chat_figures.py
        # on it and exempt its id here if it passes.
        if (table := found.get("schematize")) is not None:
            return out.to_model(skills.expand(table, args) + "\nThe reader asked for /diagram: say in the "
                                "caption that a table stands in for the diagram.")
    return out.to_model(body)
