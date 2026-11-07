"""Tests for the command registry (silica/command_table.py) and dependent surfaces."""
from __future__ import annotations

import io
import re
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit.document import Document
from rich.console import Console

from silica.command_table import COMMANDS
from silica.ui.commands import render_help
from silica.ui.home import print_home
from silica.ui.prompt import SlashCommandCompleter


def _make_console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, width=160, highlight=False), buf


# ---------------------------------------------------------------------------
# 1. Registry ↔ handler parity
# ---------------------------------------------------------------------------

def _repl_own_from_source() -> set[str]:
    """The slash commands the REPL answers itself, its literals in cli.py, with the
    persona dispatcher both surfaces ask first (ADR-0127) and /web, which is a turn
    and not a command (session.turn)."""
    root = Path(__file__).parent.parent / "silica"
    src = "".join((root / rel).read_text() for rel in ("cli.py", "kernel/personas.py", "session/turn.py"))
    found = set(re.findall(r'"(/[\w-]+)"', src))
    found.discard("/quit")   # /exit's other names: its row says so
    found.discard("/q")
    return found


def test_registry_handler_parity():
    # The Python commands only: a skill dispatches through the skill fallback,
    # never by a row (tests/test_skill_commands.py, ADR-0063).
    registry_names = {c.name for c in COMMANDS}
    own = _repl_own_from_source()
    assert own <= registry_names, f"answered in cli.py but absent from COMMANDS: {own - registry_names}"
    unanswered = {c.name for c in COMMANDS if not c.handler} - own
    assert not unanswered, f"rows with no handler that nothing answers: {unanswered}"


# ---------------------------------------------------------------------------
# 2. Completer
# ---------------------------------------------------------------------------

def _completions(text: str) -> list[tuple[str, str]]:
    """Return (name, display_meta_plain) pairs from SlashCommandCompleter for given text."""
    from prompt_toolkit.formatted_text import to_plain_text
    completer = SlashCommandCompleter()
    doc = Document(text, len(text))
    return [
        (c.text, to_plain_text(c.display_meta) if c.display_meta else "")
        for c in completer.get_completions(doc, None)
    ]


def test_completer_re_yields_report():
    names = [name for name, _ in _completions("/re")]
    assert "/report" in names


def test_completer_re_display_meta():
    meta = {name: meta for name, meta in _completions("/re")}
    report_cmd = next(c for c in COMMANDS if c.name == "/report")
    assert meta.get("/report") == report_cmd.summary


def test_completer_nuc_yields_nucleate():
    names = [name for name, _ in _completions("/nuc")]
    assert "/nucleate" in names


def test_completer_ordering_workflow_direct_system():
    all_completions = _completions("/")
    names = [name for name, _ in all_completions if name.startswith("/") and " " not in name]
    cmd_map = {c.name: c.group for c in COMMANDS}
    groups = [cmd_map[n] for n in names if n in cmd_map]

    # Groups should appear in order: workflow before direct before system
    # Find first occurrence of each group
    def first_idx(group: str) -> int:
        try:
            return groups.index(group)
        except ValueError:
            return len(groups)

    assert first_idx("workflow") < first_idx("direct"), "workflow should precede direct"
    assert first_idx("direct") < first_idx("system"), "direct should precede system"


def test_completer_no_completions_for_plain_text():
    completions = _completions("hello")
    assert completions == []


# ---------------------------------------------------------------------------
# 3. render_help()
# ---------------------------------------------------------------------------

def test_render_help_contains_all_group_headers_and_commands():
    con, buf = _make_console()
    with patch("silica.term.console.CONSOLE", con):
        render_help()
    output = buf.getvalue()

    assert "Workflow" in output
    assert "Direct" in output
    assert "System" in output
    for cmd in COMMANDS:
        assert cmd.name in output, f"{cmd.name} missing from render_help() output"


# ---------------------------------------------------------------------------
# 4. print_home()
# ---------------------------------------------------------------------------

def test_print_home_shows_model_vault_line_without_command_table():
    con, buf = _make_console()
    with patch("silica.ui.home.print_banner"), \
         patch("silica.ui.home.CONSOLE", con), \
         patch("silica.term.console.CONSOLE", con):
        print_home()
    output = buf.getvalue()

    from silica.config import CONFIG
    worker_model = CONFIG.worker_model or CONFIG.model
    worker_slug = worker_model.rsplit("/", 1)[-1]
    assert worker_slug in output
    assert "◇" in output

    # the commands overview table was dropped from the launch surface — no
    # command names appear here anymore; they live in /help.
    for cmd in COMMANDS:
        assert cmd.name not in output, f"{cmd.name} should not appear in the slimmed print_home()"


