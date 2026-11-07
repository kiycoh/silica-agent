# SPDX-License-Identifier: AGPL-3.0-or-later
"""One row per command, and what running one gives back."""
from __future__ import annotations

import ast
import importlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.errors import MarkupError

from silica.command_table import COMMANDS, ROWS, TURN_COMMANDS, Result
from silica.commands import dispatch
from silica.term.console import CONSOLE, plain

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("row", [c for c in COMMANDS if c.handler], ids=lambda c: c.name)
def test_a_row_names_a_handler_that_exists(row):
    module, name = row.handler.split(":")
    assert callable(getattr(importlib.import_module(module), name))


def test_a_row_with_no_handler_is_the_repl_s_own_or_a_turn_s():
    """What the browser would answer "not available" to must not be offered to it."""
    bare = {c.name for c in COMMANDS if not c.handler}
    assert bare - set(TURN_COMMANDS) == {c.name for c in COMMANDS if not c.handler and c.repl_only}


def test_an_old_name_runs_its_row():
    assert ROWS["/learn"] is ROWS["/memorize"] and ROWS["/q"] is ROWS["/exit"]


def test_no_command_by_that_name_is_none_and_prose_never_reaches_a_handler():
    assert dispatch("/persona-of-nobody", []) is None
    assert dispatch("what is entropy?", []) is None
    assert dispatch('it\'s a "quote', []) is None


def test_a_line_is_kept_and_said_at_once_only_where_a_surface_asks():
    said = []
    live = Result(live=lambda *a, **k: said.append(a))
    live.print("  one")
    assert said == [("  one",)] and live.lines == [(("  one",), {})]
    quiet = Result()
    quiet.print("  two", soft_wrap=True)
    assert quiet.lines == [(("  two",), {"soft_wrap": True})]


def test_bad_markup_raises_where_it_is_said_on_either_surface():
    """/find's own except answers it with the raw result, and the work after the
    line must not run: at the render it would be too late for both."""
    with pytest.raises(MarkupError):
        Result().print("  a [/x] closing nothing")
    Result().print("  a [/x] taken as written", markup=False)


def test_to_model_of_nothing_leaves_the_command_finished():
    assert Result().to_model("").directive is None and Result().to_model("go").directive == "go"


def test_the_browser_s_text_wraps_where_the_terminal_does():
    long = "  [bold]" + "word " * 30 + "[/]"
    with CONSOLE.capture() as cap:
        CONSOLE.print(long)
        CONSOLE.print("  a line kept [bold]whole[/] " + "x" * 200, soft_wrap=True)
        CONSOLE.print("  [red]no markup[/] here", markup=False)
    out = Result()
    out.print(long)
    out.print("  a line kept [bold]whole[/] " + "x" * 200, soft_wrap=True)
    out.print("  [red]no markup[/] here", markup=False)
    assert plain(out.lines) == cap.get().strip()


def test_the_turn_captures_no_console():
    """The bytes a browser shows come from the Result: a capture of the shared console
    also took whatever another thread printed meanwhile."""
    tree = ast.parse((ROOT / "silica/ui/web/turn.py").read_text(encoding="utf-8"))
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "capture"]


def test_no_handler_imports_the_console():
    for path in (ROOT / "silica/commands").glob("*.py"):
        assert "CONSOLE" not in path.read_text(encoding="utf-8"), path


def test_ctrl_c_in_a_direct_command_is_the_interrupted_line_and_the_repl_goes_on(tmp_vault, capsys):
    """It escaped main() as a raw traceback, past the session's capture, where a
    workflow command already said this."""
    session = MagicMock()
    session.prompt.side_effect = ["/embed", "/model", EOFError()]
    real = dispatch

    def interrupted(text, *a, **k):
        if text == "/embed":
            raise KeyboardInterrupt
        return real(text, *a, **k)

    with patch("silica.cli.build_session", return_value=session), patch("silica.ui.logging.setup_logging"), \
         patch("silica.cli.print_home"), patch("silica.cli.dispatch", interrupted), patch("sys.argv", ["silica"]):
        from silica.cli import main

        main()
    out = capsys.readouterr().out
    assert "(interrupted" in out and "Current model" in out
