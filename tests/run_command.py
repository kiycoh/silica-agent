# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A line through the table the way the REPL runs it, printed to `silica.cli.CONSOLE`,
for the tests that read a command's output or its directive."""
from __future__ import annotations

from silica import cli
from silica.command_table import Ask


def _ask(ask) -> Ask | None:
    """A test's answer to a surface's question, whichever kind it is put as."""
    return None if ask is None else Ask(ask, ask, ask, ask)


def direct(line: str, messages: list[dict] | None = None, ask=None) -> bool:
    """Whether the table ran `line` to its end, with nothing left for the model."""
    result = cli._command(line, [] if messages is None else messages, ask=_ask(ask))
    return result is not None and result.directive is None


def expansion(line: str, progress=None, ask=None) -> str | None:
    """What the model is asked, "" for nothing, None when no command has that name."""
    result = cli._command(line, [], ask=_ask(ask), renderer=progress)
    return None if result is None else result.directive or ""


def refusal(line: str) -> str | None:
    """Why the table will not run `line`, None when it will."""
    result = cli._command(line, [])
    return None if result is None else result.error
