"""The TUI captures its own conversations at the points where one ends."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from silica.config import CONFIG

ANSWER = "The gate defers what it cannot verify. " * 12


@pytest.fixture
def wal(tmp_path, monkeypatch):
    import silica.kernel.recall.paths as paths
    monkeypatch.setattr(paths, "_SILICA_HOME", tmp_path / "silica-home")
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(CONFIG, "vault_path", str(vault))
    monkeypatch.setattr(CONFIG, "capture_sessions", True)
    return vault


def _run(inputs):
    """Drive the REPL through `inputs`, answering each turn without an LLM."""
    session = MagicMock()
    session.prompt.side_effect = inputs

    def _answer(messages, **kw):
        messages.append({"role": "assistant", "content": ANSWER})
        return ANSWER

    with patch("silica.cli.build_session", MagicMock(return_value=session)), \
         patch("silica.cli.run_agent", _answer), \
         patch("silica.cli._model_configured", lambda: True), \
         patch("silica.cli.CONSOLE"), \
         patch("silica.cli.print_home"), \
         patch("silica.cli._setup_logging"), \
         patch("silica.cli._update_context_tokens"), \
         patch("silica.cli._ensure_servers"), \
         patch("silica.cli._announce_code_lane"), \
         patch("silica.cli._activate_repo_mode"), \
         patch("sys.argv", ["silica"]):
        from silica.cli import main
        main()


def _envelopes(vault):
    from silica.kernel.recall.paths import inbox_dir_for
    d = inbox_dir_for(str(vault))
    return sorted(p.name for p in d.glob("*.json")) if d.is_dir() else []


def _payload(vault, name):
    from silica.kernel.recall.paths import inbox_dir_for
    env = json.loads((inbox_dir_for(str(vault)) / name).read_text(encoding="utf-8"))
    return json.loads(env["payload"])


def test_leaving_the_session_captures_the_conversation(wal):
    _run(["what does the write gate defer?", "/exit"])

    (name,) = _envelopes(wal)
    assert name.endswith("-end.json")
    assert [t["role"] for t in _payload(wal, name)] == ["user", "assistant"]


def test_ctrl_d_captures_too(wal):
    """The end of a session is the end of a session, however it arrives."""
    _run(["what does the write gate defer?", EOFError()])

    assert len(_envelopes(wal)) == 1


def test_clear_captures_before_it_wipes_the_history(wal):
    _run(["what does the write gate defer?", "/clear", "/exit"])

    names = _envelopes(wal)
    assert len(names) == 1
    assert "-clear-" in names[0]  # the wiped conversation, and no empty end


def test_incognito_stops_capture_for_the_running_session(wal):
    _run(["/incognito", "what does the write gate defer?", "/exit"])

    assert _envelopes(wal) == []
