"""Silica CLI — the entry point REPL.

From SILICA.md §8.4:
  After `uv pip install -e .`, the command `silica` is in PATH.
  Opens a REPL with prompt_toolkit, runs the agentic loop.
"""
from __future__ import annotations

import logging
import sys

from rich.markdown import Markdown

from silica.agent.loop import run_agent
from silica.config import CONFIG
from silica.prompts import SYSTEM_PROMPT
from silica.ui.banner import print_banner
from silica.ui.console import CONSOLE
from silica.ui.prompt import build_session, bottom_toolbar, prompt_text

# Import tools to trigger registration via @tool decorator
import silica.tools.atomic  # noqa: F401
import silica.tools.composed  # noqa: F401
import silica.tools.wrapped  # noqa: F401

logger = logging.getLogger(__name__)


def _setup_logging(debug: bool = False) -> None:
    """Configure logging for the CLI session."""
    CONFIG.debug_logging = debug
    level = logging.DEBUG if debug else logging.WARNING
    
    # Reset existing handlers to configure cleanly
    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stderr)
    if debug:
        from silica.ui.logging import HumanFriendlyFormatter
        formatter = HumanFriendlyFormatter()
    else:
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    handler.setFormatter(formatter)
    root.addHandler(handler)
    root.setLevel(level)

    # LiteLLM/httpx/openai are always silenced — their DEBUG is raw HTTP/request dumps
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("litellm").setLevel(logging.WARNING)
    logging.getLogger("LiteLLM").setLevel(logging.ERROR)
    logging.getLogger("openai").setLevel(logging.WARNING)


def _handle_slash_command(cmd: str, messages: list[dict]) -> bool:
    """Handle slash commands. Returns True if the command was handled."""
    cmd = cmd.strip().lower()

    if cmd in ("/exit", "/quit", "/q"):
        return False  # Signal to exit

    if cmd == "/model":
        CONSOLE.print(f"  Current model: [bold]{CONFIG.model}[/]")
        return True

    if cmd == "/tools":
        from silica.tools import TOOLS
        if not TOOLS:
            CONSOLE.print("  No tools registered.")
        else:
            CONSOLE.print(f"  [bold]{len(TOOLS)} registered tools:[/]")
            for name, t in sorted(TOOLS.items()):
                CONSOLE.print(f"    [dim]\\[{t.cls}][/] {name}")
        return True

    if cmd == "/help":
        CONSOLE.print("  [bold cyan]/exit[/]     — exit silica")
        CONSOLE.print("  [bold cyan]/model[/]    — show current LLM model")
        CONSOLE.print("  [bold cyan]/tools[/]    — list registered tools")
        CONSOLE.print("  [bold cyan]/clear[/]    — reset conversation history")
        CONSOLE.print(f"  [bold cyan]/verbose[/]  — cycle tool progress: off → new → all → verbose  [dim](current: {CONFIG.tool_progress})[/]")
        CONSOLE.print("  [bold cyan]/thinking[/] — toggle reasoning block display")
        CONSOLE.print("  [bold cyan]/help[/]     — show this help message")
        return True

    if cmd == "/thinking":
        CONFIG.show_thinking = not CONFIG.show_thinking
        state = "on" if CONFIG.show_thinking else "off"
        CONSOLE.print(f"  Thinking display: [bold]{state}[/]")
        return True

    if cmd == "/verbose":
        from typing import Literal
        modes: tuple[Literal["off", "new", "all", "verbose"], ...] = ("off", "new", "all", "verbose")
        current = CONFIG.tool_progress
        next_mode = modes[(modes.index(current) + 1) % len(modes)]
        CONFIG.tool_progress = next_mode
        CONSOLE.print(f"  Tool progress: [bold]{next_mode}[/]")

        if next_mode == "verbose":
            _setup_logging(debug=True)
            CONSOLE.print("  System log level: [bold]DEBUG[/]")
        else:
            _setup_logging(debug=False)
            CONSOLE.print("  System log level: [bold]WARNING[/]")

        return True

    CONSOLE.print(f"  Unknown command: {cmd}. Use [bold cyan]/help[/] to see options.")
    return True


def main():
    """Entry point for the `silica` CLI command."""
    debug_mode = "--verbose" in sys.argv or "-v" in sys.argv or CONFIG.debug_logging
    _setup_logging(debug=debug_mode)

    print_banner()
    CONSOLE.print(f"  Model: [bold]{CONFIG.model}[/]")
    if CONFIG.vault_name:
        CONSOLE.print(f"  Vault:   [bold]{CONFIG.vault_name}[/]")
    CONSOLE.print()

    session = build_session()
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    from silica.agent.progress import make_progress_callback
    callback = make_progress_callback()

    while True:
        try:
            user_input = session.prompt(prompt_text(), bottom_toolbar=bottom_toolbar)
        except (EOFError, KeyboardInterrupt):
            print("\n  Goodbye.")
            break

        user_input = user_input.strip()
        if not user_input:
            continue

        # Handle slash commands
        if user_input.startswith("/"):
            cmd = user_input.strip().lower()
            if cmd == "/clear":
                CONSOLE.clear()
                print_banner()
                CONSOLE.print(f"  Model: [bold]{CONFIG.model}[/]")
                if CONFIG.vault_name:
                    CONSOLE.print(f"  Vault:   [bold]{CONFIG.vault_name}[/]")
                CONSOLE.print()

                messages.clear()
                messages.append({"role": "system", "content": SYSTEM_PROMPT})
                session = build_session()
                continue

            should_continue = _handle_slash_command(user_input, messages)
            if not should_continue:
                print("  Goodbye.")
                break
            continue

        # Normal user message → agentic loop
        messages.append({"role": "user", "content": user_input})

        try:
            answer = run_agent(messages, model=CONFIG.model, tool_progress_callback=callback)
            if answer:
                CONSOLE.rule(style="dim")
                CONSOLE.print(Markdown(answer))
                CONSOLE.print()
            messages.append({"role": "assistant", "content": answer or ""})
        except KeyboardInterrupt:
            CONSOLE.print("\n  [dim](interrupted)[/]")
        except Exception as e:
            logger.exception("Agent error")
            CONSOLE.print(f"\n  [bold red]Error:[/] {e}\n")


if __name__ == "__main__":
    main()
