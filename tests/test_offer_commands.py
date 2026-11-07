# SPDX-License-Identifier: AGPL-3.0-or-later
"""The study offers write command lines into a button, and the handlers read them back:
two files that agree on a string only because a test says so."""
from __future__ import annotations

import shlex

import pytest

from silica.command_table import ROWS
from silica.kernel.learn import offer
from silica.session.words import int_flag, positional

NAME = "Rete neurale (2)"


def _lines() -> list[str]:
    forms = [line.format(name=NAME) for _label, line in offer._FORM_COMMANDS.values()]
    note = "# T\n\n" + "".join(f"## H{i}\n\n- [[A{i}]]\n- [[B{i}]]\n- step\n\n" for i in range(4))
    aids = [a["command"] for state in ("missed", "lost", "blind", "fragile")
            for a in offer.aids("ML/Rete neurale (2).md", NAME, state, note)]
    return forms + aids


@pytest.mark.parametrize("line", sorted(set(_lines())))
def test_every_offered_line_is_a_command_that_reads_its_target_back(line, tmp_vault):
    from silica.config import CONFIG
    from silica.ui.commands import command_names
    from silica_core.kernel.skills import discover

    words = shlex.split(line)
    assert words[0] in ROWS or words[0][1:] in discover(CONFIG.vault_path or None), command_names()
    assert positional(words[1:]) and NAME in positional(words[1:])[0]
    if "--n=" in line:
        assert int_flag(words[1:], "--n=", -1) > 0
