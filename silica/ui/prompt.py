# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

from __future__ import annotations

import html
import os
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion, FuzzyCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.styles import Style

from silica.config import CONFIG
from silica.ui.commands import commands, unrecorded


def _vault_display() -> str:
    if CONFIG.vault_path:
        return Path(CONFIG.vault_path).name
    return CONFIG.vault_name or "—"


def _markup_safe(value: str) -> str:
    """Escape a value before it is interpolated into a prompt_toolkit HTML string.

    HTML() parses its argument as XML, so a folder named "R&D" or a model id
    holding an angle bracket raises ExpatError on every REPL iteration — and the
    call site only guards EOFError/KeyboardInterrupt, so the REPL dies at startup
    and stays dead until the folder is renamed.
    """
    return html.escape(value)


_METER_WIDTH = 10


def _history_path() -> Path:
    p = Path.home() / ".silica"
    p.mkdir(parents=True, exist_ok=True)
    path = p / "history"
    # 0600 from the first byte: FileHistory appends with the umask's mode, and
    # every line ever typed is in this file. An older file is brought down too.
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
    path.chmod(0o600)
    return path


class _History(FileHistory):
    """The REPL's history, minus every line a command marks as able to carry
    a secret (`commands.unrecorded`): not in the file, and not in this
    session's up-arrow either, so it cannot be recalled and re-sent."""

    def append_string(self, string: str) -> None:
        if unrecorded(string):
            return
        super().append_string(string)


def _context_meter() -> str:
    """Return a prompt_toolkit HTML snippet with a █/░ fill bar for context usage."""
    if CONFIG.max_context_tokens <= 0:
        return ""
    ratio = min(1.0, max(0.0, CONFIG.context_tokens / CONFIG.max_context_tokens))
    filled = round(ratio * _METER_WIDTH)
    bar = "█" * filled + "░" * (_METER_WIDTH - filled)
    color = "ansicyan" if ratio < 0.7 else ("ansiyellow" if ratio < 0.9 else "ansired")
    pct = round(ratio * 100)
    return f" <{color}>{bar}</{color}> <ansigray>{pct}%</ansigray> "


def bottom_toolbar() -> HTML:
    # Vault lives in the prompt line — not repeated here. Model shown as slug.
    model_slug = _markup_safe((CONFIG.model or "—").rsplit("/", 1)[-1])
    think = "thinking:on" if CONFIG.show_thinking else "thinking:off"
    progress = f"progress:{CONFIG.tool_progress}"
    meter = _context_meter()
    return HTML(
        f" <ansicyan><b>{model_slug}</b></ansicyan>  "
        f"{progress}  "
        f"<b>{think}</b>"
        f"{meter}"
    )


class SlashCommandCompleter(Completer):
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor.lstrip()
        if not text.startswith("/"):
            return
        try:
            for cmd in commands():
                if cmd.name.startswith(text):
                    yield Completion(
                        cmd.name,
                        start_position=-len(text),
                        display=cmd.name,
                        display_meta=cmd.summary,
                    )
                for ex in cmd.examples:
                    if ex.startswith(text):
                        yield Completion(
                            ex,
                            start_position=-len(text),
                            display=ex,
                            display_meta=cmd.summary,
                        )
        except Exception:
            return


def build_session() -> PromptSession:
    return PromptSession(
        history=_History(str(_history_path())),
        auto_suggest=AutoSuggestFromHistory(),
        completer=FuzzyCompleter(SlashCommandCompleter()),
        # Kill the default reverse-video strip — the toolbar renders flat on the
        # terminal background, colors come from the HTML fragments themselves.
        style=Style.from_dict({"bottom-toolbar": "noreverse"}),
    )


def prompt_text() -> HTML:
    vault = _vault_display()
    if vault != "—":
        return HTML(
            f"<ansicyan><b>silica</b></ansicyan> "
            f"<ansigray>[{_markup_safe(vault)}]</ansigray> › "
        )
    return HTML("<ansicyan><b>silica</b></ansicyan> › ")
