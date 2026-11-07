# SPDX-License-Identifier: AGPL-3.0-or-later
"""A line the handler cannot run is shown to the person and the model is never asked:
a usage error used to go to the model as a directive on both surfaces, whatever the
comment over it said."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from silica.cli import main

REFUSED = [
    ('/quiz "Alpha', "unbalanced quotes"),
    ("/convert", "/convert requires at least one file path"),
    ("/web-search", "/web-search requires a concept"),
    ("/fetch", "/fetch requires a URL"),
    ("/slides", "/slides needs a note or a folder"),
    ("/flashcards", "/flashcards needs a note or a folder"),
    ("/memorize --teach", "/memorize --teach requires a note"),
    ("/memorize --plan", "/memorize --plan requires a name"),
    ("/memorize --plan P", "/memorize --plan needs --scope"),
    ("/enrich NoSuchFolder", "no files found in 'NoSuchFolder'"),
    ("/settings bogus.key x", "usage /settings"),
    ("/compare", "compare"),
]


def _no_model(*a, **k):
    raise AssertionError("the model was asked")


@pytest.mark.parametrize("line,says", REFUSED)
def test_the_repl_shows_it_and_asks_no_model(tmp_vault, capsys, line, says):
    session = MagicMock()
    session.prompt.side_effect = [line, EOFError()]
    with patch("silica.cli.build_session", return_value=session), \
         patch("silica.cli._setup_logging"), patch("silica.cli.print_home"), \
         patch("silica.session.turn.run_agent", _no_model), patch("sys.argv", ["silica"]):
        main()
    assert says in capsys.readouterr().out.replace("\n", "")


@pytest.mark.parametrize("line,says", REFUSED)
def test_the_browser_shows_it_and_asks_no_model(tmp_vault, monkeypatch, line, says):
    from silica.ui.web import turn
    from silica.ui.web.session import SESSION

    SESSION.reset()
    monkeypatch.setattr("silica.session.turn.run_agent", _no_model)

    async def frames():
        return [x async for x in turn.run_turn(turn.Ask(line))]

    last = asyncio.run(frames())[-1]
    assert last["type"] == "error" and says in last["error"]
    assert not any(m.get("role") == "user" for m in SESSION.chat.messages)
