"""The REPL's prompt history (~/.silica/history) never holds a secret.

The tester's steps (ADR-0091, 2026-10-12): `/connect add` with an inline
`API_KEY` typed at the prompt put the key on a line of the history file, and
the file was mode 664. A line a command marks as able to carry a secret is
never recorded, and the file is created and kept at 0600.
"""
from __future__ import annotations

import stat

import pytest

_SECRET = "tui-inline-secret-5150"
_ADD = '/connect add {"tk":{"command":"x","env":{"API_KEY":"%s"}}}' % _SECRET


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _type_lines(*lines: str):
    """Each line typed at a real PromptSession and accepted with Enter, as the
    REPL's loop does; returns the session."""
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from silica.ui import prompt

    with create_pipe_input() as inp, create_app_session(input=inp, output=DummyOutput()):
        session = prompt.build_session()
        for line in lines:
            inp.send_text(line + "\r")
            assert session.prompt("> ") == line
    return session


def test_a_connect_add_line_never_reaches_the_history_file(home):
    session = _type_lines(_ADD, "/connect list")
    history = (home / ".silica" / "history").read_text(encoding="utf-8")
    assert "/connect list" in history
    assert _SECRET not in history
    assert _ADD not in list(session.history.get_strings())  # not even for the up arrow


def test_a_connect_secure_line_is_not_recorded_either(home):
    _type_lines("/connect secure gh", "/connect login gh")
    history = (home / ".silica" / "history").read_text(encoding="utf-8")
    assert "/connect secure" not in history
    assert "/connect login gh" in history


def test_the_history_file_is_created_0600(home):
    _type_lines("/connect list")
    mode = stat.S_IMODE((home / ".silica" / "history").stat().st_mode)
    assert mode == 0o600


def test_an_existing_history_file_is_brought_to_0600(home):
    path = home / ".silica" / "history"
    path.parent.mkdir(parents=True)
    path.write_text("\n# old\n+/status\n", encoding="utf-8")
    path.chmod(0o664)
    _type_lines("/status")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_rule_is_named_on_the_command_not_matched_by_hand():
    from silica.ui.commands import unrecorded

    assert unrecorded(_ADD)
    assert unrecorded("  /connect   secure gh")
    assert not unrecorded("/connect login gh")
    assert not unrecorded("/connect")
    assert not unrecorded("connect add {}")  # chat text, not the command
    assert not unrecorded("/connector add")
