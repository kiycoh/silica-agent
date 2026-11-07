# SPDX-License-Identifier: AGPL-3.0-or-later
"""The door from a turn to the commands, which the CLI fills when it loads."""
from __future__ import annotations

import pytest

from silica.session import commands


def test_a_turn_with_no_command_table_says_so(monkeypatch):
    monkeypatch.setattr(commands, "_dispatch", None)
    with pytest.raises(RuntimeError, match="silica.cli"):
        commands.dispatch("/status", [])


def test_the_cli_fills_the_door():
    """"" says the command did it all, None that no command has that name."""
    assert commands.dispatch("/persona-of-nobody", []) is None
    assert commands.dispatch("what is entropy?", []) is None
