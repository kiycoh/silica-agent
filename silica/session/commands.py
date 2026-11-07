# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The one door from a turn to the slash commands.

The commands' handlers sit in `silica/cli.py`, above every surface, so a surface
cannot import them; the CLI hands its dispatcher in here when it loads, and every
process that runs a turn starts in it (`silica`, `silica --gui`, `silica connect`).

The two surfaces walk the same tables in a different order, and that is what
each of them does today, kept and named here rather than left to read as the
same one. The REPL asks the direct handler, then the expansion, then `/web`, then
its own meta commands, and hands an unknown command to the model. `run_turn` asks
the withheld list, `/web`, then this door, and refuses an unknown command. Only a
skill named `web` tells the two orders apart: the REPL runs it, the browser the
web turn.
"""
from __future__ import annotations

from collections.abc import Callable

Dispatch = Callable[..., "str | None"]

_dispatch: Dispatch | None = None


def install(dispatch: Dispatch) -> None:
    global _dispatch
    _dispatch = dispatch


def dispatch(text: str, messages: list[dict], *, ask=None, progress=None) -> str | None:
    """Run the command `text` names. `""`: it did the whole job and there is
    nothing left for the model. A non-empty string: the directive the model is
    asked instead. None: no command by that name.

    `ask` is the surface's way to put a question to the person at it, None when
    there is nobody to ask. `progress` is a surface's own renderer."""
    if _dispatch is None:
        raise RuntimeError("no command table: silica.cli was never loaded")
    return _dispatch(text, messages, ask=ask, progress=progress)
