"""/agenda is a registered direct command (renders without an LLM turn).

repl_only: the web GUI has its own calendar tab, so the command stays out
of the GUI command picker — same policy as the other terminal-session
affordances.
"""
from __future__ import annotations


def test_agenda_command_registered_direct_and_repl_only():
    from silica.ui.commands import COMMANDS

    [cmd] = [c for c in COMMANDS if c.name == "/agenda"]
    assert cmd.group == "direct"
    assert cmd.repl_only is True
