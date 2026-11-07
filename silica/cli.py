# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Silica CLI — the entry point REPL.

From SILICA.md §8.4:
  After `uv pip install -e .`, the command `silica` is in PATH.
  Opens a REPL with prompt_toolkit, runs the agentic loop.
"""
from __future__ import annotations

import json
import logging
import os
import signal
from pathlib import Path
import sys
import threading
import uuid
from collections.abc import Callable
from contextlib import nullcontext, redirect_stdout

from rich.markup import escape

from silica.term.style import FlatMarkdown

from silica.agent.recall_watch import numbers_of_session
from silica.command_table import Ask, Result
from silica.commands import dispatch
from silica.config import claim_env, CONFIG, GUI_PORT, live_env
from silica.onboarding.adopt import resolve_cwd_vault, resolve_vault_switch
from silica.session import meter, seed
from silica.session import turn as session_turn
from silica.session.conversation import Conversation
from silica.session.words import positional
from silica.term.console import CONSOLE
from silica.ui.home import print_home
from silica.ui.prompt import build_session, bottom_toolbar, prompt_text
from prompt_toolkit.patch_stdout import patch_stdout

from silica.tools import catalog

catalog.load()

logger = logging.getLogger(__name__)


class _TurnInterrupt:
    """Ctrl+C during a TUI turn (ADR-0004), and the turn's cancel token.

    During a tool the first press only sets the token: a recipe tool's worker
    pools poll it between items and drain, where a KeyboardInterrupt lands in
    the main thread mid-run and leaves those pools running. Anywhere else the
    main thread is only waiting on the model's daemon thread, so raising there
    stops at once, as it always did; the token is set first so nothing the turn
    started keeps going. A second press raises even during a tool."""

    def __init__(self, forward: Callable) -> None:
        self.token = threading.Event()
        self._forward = forward
        # A counter and not a flag: a recipe tool runs a loop of its own, whose
        # tool events reach this same callback inside the outer tool's, from its
        # worker threads too, hence the lock.
        self._tools = 0
        self._lock = threading.Lock()

    def __call__(self, event) -> None:
        from silica.obs.events import ToolCompleteEvent, ToolErrorEvent, ToolStartEvent

        if isinstance(event, ToolStartEvent):
            with self._lock:
                self._tools += 1
        elif isinstance(event, (ToolCompleteEvent, ToolErrorEvent)):
            with self._lock:
                self._tools = max(self._tools - 1, 0)
        self._forward(event)

    def on_sigint(self, signum, frame) -> None:
        first = not self.token.is_set()
        self.token.set()
        if first and self._tools:
            CONSOLE.print("\n  [dim](stopping once the running step lets go; Ctrl+C again to force)[/]")
            return
        raise KeyboardInterrupt


def _print_turn_notes(notes: list[dict]) -> None:
    """The lines under an answer, dim, in the channel's order (ADR-0094 point 8):
    the same notes the GUI draws, which arrive already ordered by attach()."""
    from rich.markup import escape

    if not notes:
        return
    for note in notes:
        CONSOLE.print(f"  [dim]{escape(note['text'])}[/]")
    CONSOLE.print()


def _print_answer(t: session_turn.Turn) -> None:
    if t.answer:
        CONSOLE.print()
        CONSOLE.print("[role.assistant]⏺ silica[/]")
        CONSOLE.print(FlatMarkdown(t.answer))
        CONSOLE.print()
    _print_turn_notes(t.notes)


_BEFORE = (session_turn.name_notes,)
_AFTER = (session_turn.check_numbers, session_turn.attribute, session_turn.note, session_turn.beat,
          _print_answer, session_turn.recount)


def _agent_turn(t: session_turn.Turn, callback) -> None:
    """The model's part of a REPL turn and everything after it, on this thread."""
    interrupt = _TurnInterrupt(callback)
    session_turn.watched(t, interrupt, surface="repl")
    # Only around the turn: at the prompt, prompt_toolkit owns Ctrl+C.
    previous_sigint = signal.signal(signal.SIGINT, interrupt.on_sigint)
    try:
        session_turn.ask_model(t, interrupt.token, session_turn.constraints(t))
        if interrupt.token.is_set():
            # A drained tool hands back the loop's "(silica: cancelled)",
            # which is no answer: the turn ends the way a raise ends it.
            raise KeyboardInterrupt
        for step in _AFTER:
            step(t)
    except KeyboardInterrupt:
        callback.close()
        from silica.obs import narration as _narr
        _narr.NARRATOR.cancel(driver="tui", target=None, scope="turn")
        CONSOLE.print("\n  [dim](interrupted)[/]")
    except Exception as e:
        callback.close()
        logger.exception("Agent error")
        CONSOLE.print(f"\n  [bold red]Error:[/] {e}\n")
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
    # After the except arms too: an interrupted or failed turn still read
    # what its results showed, and the narration /resume rebuilds from has them.
    assert t.read is not None  # `watched` above
    t.conv.seen_numbers |= t.read.numbers


def _setup_logging(debug: bool = False) -> None:
    """Configure logging for the CLI session."""
    import threading
    CONFIG.debug_logging = debug
    level = logging.DEBUG if debug else logging.WARNING

    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)
        if isinstance(h, logging.FileHandler):
            h.close()   # a re-setup (tests, /verbose) must not leak the previous fd

    handler: logging.Handler
    from silica.config import hosted
    if hosted():
        from silica.ui.logging import HostedJsonFormatter, LiveAwareStreamHandler
        # A hosted tenant's stderr is its journal, which the privacy notice
        # declares (ADR-0100 point 2): templates, never arguments, whatever
        # --verbose or SILICA_DEBUG ask for, since DEBUG records carry the
        # model's reasoning and tool arguments. The handler opens at INFO only
        # for silica.metrics, whose logger is raised to INFO here because root
        # stays at WARNING now that the hosted file log is gone.
        handler = LiveAwareStreamHandler()
        handler.setFormatter(HostedJsonFormatter())
        handler.setLevel(logging.INFO)
        handler.addFilter(lambda r: r.levelno >= logging.WARNING or r.name == "silica.metrics")
        logging.getLogger("silica.metrics").setLevel(logging.INFO)
        level = logging.WARNING
    elif debug:
        from rich.logging import RichHandler
        from silica.ui.logging import (
            AnsiHumanFriendlyFormatter,
            HumanFriendlyFormatter,
            LiveAwareStreamHandler,
        )
        handler = RichHandler(
            console=CONSOLE,
            markup=True,
            show_path=False,
            show_level=False,
            show_time=False,
        )
        handler.setFormatter(HumanFriendlyFormatter())
        # Rich's Live display is driven from the main thread; worker threads logging
        # through RichHandler concurrently corrupt the terminal render state.
        # Restrict RichHandler to the main thread only.
        main_thread = threading.main_thread()
        handler.addFilter(lambda r: threading.current_thread() is main_thread)

        # Worker-thread records fall back to a live-aware stderr handler: resolving
        # sys.stderr at emit time follows rich.Live's redirect, so they print above
        # an active live region instead of tearing it (stale-frame duplication).
        bg_handler = LiveAwareStreamHandler()
        # Same human-friendly seam as the main thread — rendered to ANSI in the
        # formatter (throwaway Console) so worker logs (dedup, refine, enrich,
        # expand, orphan…) read like the main-thread ones instead of raw dumps.
        bg_handler.setFormatter(AnsiHumanFriendlyFormatter())
        bg_handler.addFilter(lambda r: threading.current_thread() is not main_thread)
        root.addHandler(bg_handler)
    else:
        from silica.ui.logging import AnsiHumanFriendlyFormatter, LiveAwareStreamHandler
        # Live-aware: follows rich.Live's stderr redirect so warnings during the
        # injector/batch live region print above it instead of tearing the panel.
        # Same human-friendly ANSI seam as debug mode's worker handler, so
        # warnings/errors (incl. worker threads like dedup) render coloured instead
        # of raw dumps. Level stays WARNING here — only warn/error surface.
        handler = LiveAwareStreamHandler()
        handler.setFormatter(AnsiHumanFriendlyFormatter())
    root.addHandler(handler)
    root.setLevel(level)
    _attach_file_log()

    # LiteLLM/httpx/openai/httpcore are always silenced — their DEBUG is raw HTTP/request dumps
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("litellm").setLevel(logging.WARNING)
    logging.getLogger("LiteLLM").setLevel(logging.ERROR)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("markdown_it").setLevel(logging.WARNING)
    # websockets DEBUG is the raw bridge handshake + per-frame dump; connect.py
    # already logs the meaningful lifecycle (connect/disconnect/refusals) itself.
    logging.getLogger("websockets").setLevel(logging.WARNING)
    # asyncio DEBUG is one "Using selector" line per event loop — litellm's sync
    # streaming path creates a fresh loop PER CHUNK, so --verbose drowns in them.
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def _attach_file_log() -> None:
    """Every silica record at DEBUG into ~/.silica/logs/silica.log, whatever
    the terminal shows. Before this the agent had no log file at all: `-v`
    went to stderr under a Rich Live region, and a run under setsid lost it
    entirely (audit 2026-09-06). Third-party loggers stay at WARNING in the
    file: root at DEBUG would otherwise let PIL/urllib3 flood it.

    ponytail: one file shared by every silica process (REPL, GUI) in
    append mode; a rotation by one process leaves the others writing to the
    renamed file until they rotate themselves. Per-pid files if that ever
    loses a line that mattered.
    """
    from logging.handlers import RotatingFileHandler
    from silica.config import hosted
    from silica.config import silica_home

    # A hosted tenant keeps no file of its own (ADR-0100 point 1): the privacy
    # notice promises the system journal and 14 days, and this file held the
    # model's reasoning and every tool call's arguments with no age limit. Its
    # WARNING lines still reach the journal through the stderr handler.
    if hosted():
        return
    root = logging.getLogger()
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return
    # Handlers without a level of their own showed what root allowed: pin that
    # before root opens up, or the terminal starts printing DEBUG.
    shown = root.level or logging.WARNING
    for h in root.handlers:
        if h.level == logging.NOTSET:
            h.setLevel(shown)
    path = silica_home() / "logs" / "silica.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(path, maxBytes=10_000_000, backupCount=3, encoding="utf-8")
    except OSError as e:
        logging.getLogger(__name__).warning("file log unavailable at %s: %s", path, e)
        return
    fh.setLevel(logging.DEBUG)
    fh.addFilter(lambda r: r.name.startswith("silica") or r.levelno >= logging.WARNING)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname).1s %(name)s [%(threadName)s] %(message)s"))
    root.addHandler(fh)
    root.setLevel(logging.DEBUG)


def default_user_vault(home=None):
    """Stable per-user vault used when no explicit SILICA_VAULT and no repo
    mode applies. Sits alongside ~/.silica/{ledger,undo_journal,checkpoints}.db.
    """
    from pathlib import Path

    return (home or Path.home()) / ".silica" / "vault"


def _activate_repo_mode() -> None:
    """Side-effecting startup vault selection: the working directory wins.

    An *exported* SILICA_VAULT outranks it (`config.VAULT_PINNED` — the pin for
    headless runs like cron, which start wherever the scheduler put them); one
    in a .env file is ignored by config.load_user_env. Where cwd is not a vault
    ($HOME), the export is the fallback, then a stable ~/.silica/vault.

    Cross-project personal memory is the separate SILICA_MEMORY_VAULT axis
    (`kernel/recall/memory_lane.py`), which self-disables inside its own vault.
    """
    from pathlib import Path
    from silica.config import VAULT_PINNED
    from silica.onboarding.adopt import declare_write_dir, seed_silicaignore

    target = None if VAULT_PINNED else resolve_cwd_vault(Path.cwd())
    target = target or CONFIG.vault_path.strip()
    if target:
        t = resolve_vault_switch(target)
        if t.error:
            CONSOLE.print(f"  [red]{target} cannot be a vault — {t.error}[/]")
            return
        if t.created:
            Path(t.vault).mkdir(parents=True, exist_ok=True)
        CONFIG.vault_path = t.vault
        declared = declare_write_dir(t.vault)
        seeded = seed_silicaignore(t.vault)
        CONSOLE.print(f"  Vault: [bold]{t.vault}[/]")
        if declared:
            CONSOLE.print(f"  Writes confined to [bold]{declared}/[/] (`write_dir` in vault.yaml).")
        if seeded:
            CONSOLE.print("  Created [bold].silicaignore[/] — add folders to keep out of the index.")
        return
    # $HOME with nothing configured → stable home vault.
    home_vault = default_user_vault()
    home_vault.mkdir(parents=True, exist_ok=True)
    CONFIG.vault_path = str(home_vault)
    CONSOLE.print(f"  Vault: [bold]{home_vault}[/]")


def _announce_code_lane() -> None:
    """Eager repo-root resolution (ADR-0019): validate the vault⊂repo invariant
    once at startup / vault switch and surface a violation loudly."""
    from silica.kernel.base.roots import repo_root_warning

    warn = repo_root_warning(CONFIG.vault_path)
    if warn:
        CONSOLE.print(f"  [yellow]⚠ {warn}[/]")


# A study artifact from the notes, beside /quiz: the deck and the cards
# NotebookLM's Studio makes (owner's comparison, 2026-10-23), both drawn from
# Learn's checked key points (learn.artifacts.study_points). The cards are made
# as data and checked before they are shown (learn.artifacts.flashcards); the
# deck is written by the agent and its points are checked after it.


def _hidden(prompt: str) -> str:
    from prompt_toolkit import prompt as ask

    return ask(prompt, is_password=True)


def _yes(prompt: str) -> bool:
    from prompt_toolkit import prompt as ask

    return ask(prompt).strip().lower() in ("y", "yes")


def _line(prompt: str) -> str:
    from prompt_toolkit import prompt as ask

    return ask(prompt)


def _command(text: str, messages: list[dict], *, ask: Ask | None = None, renderer=None) -> Result | None:
    """A line through the table, drawn here: what it says as it says it, and how
    it ended. `renderer` is the REPL's own, reused by a command that runs an agent
    loop: a `_ProgressRenderer` claims the module-global batch hook and subscribes
    to the BUS for its whole life, and only close() clears them, so a throwaway one
    left the hook on an orphan and /enrich and /dedup lost their batch panel for
    the rest of the session."""
    result = dispatch(text, messages, ask=ask, progress=lambda out: nullcontext(renderer), live=CONSOLE.print)
    if result is None:
        return None
    if result.error is not None:
        CONSOLE.print(f"  [yellow]{escape(result.error)}[/]")
    if result.answer is not None:
        CONSOLE.print(FlatMarkdown(result.answer))
    if result.batch is not None:
        from silica.ui.renderer import emit_batch_event

        emit_batch_event(result.batch)
    return result


def _handle_slash_command(cmd: str, messages: list[dict]) -> bool | None:
    """Handle a meta slash command. True = handled, False = exit the REPL,
    None = not a recognized command (the caller hands it to the agent)."""
    cmd = cmd.strip().lower()

    if cmd in ("/exit", "/quit", "/q"):
        return False  # Signal to exit

    if cmd.startswith("/sessions"):
        from silica.obs import narration as _narr
        arg = cmd.split(None, 2)[1:] if len(cmd.split()) > 1 else []
        if arg and arg[0] == "prune":
            # Deletion is the user's sentence to write: an explicit age, no
            # default (ticket 06).
            if len(arg) < 2 or not arg[1].rstrip("d").isdigit():
                CONSOLE.print("  usage: /sessions prune <days>d   e.g. /sessions prune 90d")
                return True
            n = _narr.prune(float(arg[1].rstrip("d")))
            CONSOLE.print(f"  pruned {n} narration session(s)")
            return True
        rows = _narr.list_sessions(CONFIG.vault_path or "")
        if not rows:
            CONSOLE.print("  no saved sessions for this vault")
            return True
        import datetime as _dt
        for i, r in enumerate(rows[:20], 1):
            when = _dt.datetime.fromtimestamp(r["updated"]).strftime("%Y-%m-%d %H:%M")
            tag = "" if r["store"] == "narration" else " [dim](legacy)[/]"
            CONSOLE.print(f"  {i:>2}. [bold]{r['title']}[/]{tag}  [dim]{when} · {r['id']}[/]")
        CONSOLE.print("  [dim]/resume <n|id> to reopen[/]")
        return True

    if cmd.startswith("/resume"):
        from silica.obs import narration as _narr
        parts = cmd.split()
        if len(parts) != 2:
            CONSOLE.print("  usage: /resume <n|id>  (list with /sessions)")
            return True
        rows = _narr.list_sessions(CONFIG.vault_path or "")
        sel = parts[1]
        sid = (rows[int(sel) - 1]["id"] if sel.isdigit() and 0 < int(sel) <= len(rows)
               else sel)
        replayed = _narr.load_session_messages(sid, CONFIG.vault_path or "")
        if replayed is None:
            CONSOLE.print(f"  no such session: {sel}")
            return True
        row = next((r for r in rows if r["id"] == sid), None)
        if row and row["store"] == "narration":
            try:
                _narr.NARRATOR.resume(sid)
            except _narr.SessionBusy as e:
                CONSOLE.print(f"  [bold red]{e}[/]")
                return True
        else:
            # Legacy snapshot: continue it as a NEW narration session seeded
            # with its turns — emit new, recognise legacy (ticket 05).
            _narr.NARRATOR.close()
            _narr.NARRATOR.ensure_session(driver="tui")
            for m in replayed:
                _narr.NARRATOR.turn(m)
        messages[:] = seed.fresh_messages() + replayed
        # The persona its last turn wore (ADR-0127 point 3), as the GUI's
        # /session/load puts it back.
        from silica.kernel import personas

        last: dict = next((m for m in reversed(replayed) if m.get("role") == "user"), {})
        if (worn := personas.available(CONFIG.vault_path or None).get(str(last.get(personas.TURN_KEY) or ""))):
            personas.apply(messages, worn)
        meter.update_context_tokens(messages)
        CONSOLE.print(f"  resumed [bold]{(row or {}).get('title', sid)}[/] "
                      f"({len(replayed)} message(s))")
        return True

    if cmd == "/model":
        if not CONFIG.model:
            CONSOLE.print("  Current model: [bold](not configured)[/]")
            return True
        from silica.model.limits import model_limits
        window, out_cap = model_limits(CONFIG.provider, CONFIG.model)
        extra = ""
        if window:
            extra = f"  [dim]ctx {window:,}[/]"
            if out_cap:
                extra += f" [dim]· max out {out_cap:,}[/]"
        CONSOLE.print(f"  Current model: [bold]{CONFIG.model}[/]{extra}")
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
        from silica.ui.commands import render_help
        render_help()
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

    return None  # unrecognized: the caller lets the agent infer the intent


_NO_MODEL_HINT = (
    "  [yellow]No chat model configured.[/] Run [bold]silica init[/] to set one — "
    "direct commands (/find, /status, /cooccur, …) still work."
)


def _model_configured() -> bool:
    return bool(CONFIG.model.strip())


def _doctor_live_probe() -> bool:
    """`silica doctor --live`: one tiny real completion so a green report proves
    the model actually answers. The probe itself lives in onboarding.checks
    (live_probe) so the `silica_doctor(live=True)` tool is the same check —
    this wrapper only renders it. Skipped-without-a-model stays True, as before:
    an unconfigured model is init's problem, not a failing endpoint."""
    from silica.onboarding.checks import live_probe

    if _model_configured():
        CONSOLE.print(f"  [dim]→ live probe: asking {CONFIG.model} to reply…[/]")
    r = live_probe(CONFIG)
    if r.status == "ok":
        CONSOLE.print(f"  [green]✓ live probe:[/] {r.detail}")
        return True
    if r.status == "warn":
        CONSOLE.print(f"  [yellow]⚠ live probe {r.detail}[/]")
        return True
    CONSOLE.print(f"  [red]✗ live probe:[/] {r.detail}")
    return False


def _autolaunch_wizard_if_unconfigured() -> None:
    """First run with no model: launch the wizard, then re-exec so the new config
    takes effect. Non-tty (script/CI/pipe) or an already-relaunched child skips
    this — the caller then prints the hint and drops into the offline REPL."""
    if _model_configured():
        return
    if not sys.stdin.isatty() or live_env("SILICA_WIZARD_DONE"):
        return
    import silica.onboarding.wizard as wizard_mod
    if wizard_mod.run_wizard() != 0:
        return  # aborted / failed → no re-exec, fall back to the hint
    # re-exec rather than reload — CONFIG is a module-level singleton
    # imported by value across the codebase, so reassigning it wouldn't reach
    # those aliases. execve inherits the wizard's os.environ updates; the guard
    # env var stops an infinite relaunch if config still doesn't resolve.
    claim_env({"SILICA_WIZARD_DONE": "1"})
    os.execve(sys.executable, [sys.executable, *sys.argv], os.environ)


def _ensure_servers() -> None:
    """Start the local model servers named in the env. No-op when none are."""
    from silica.onboarding.serve import ensure_local_servers
    ensure_local_servers()


_CLI_HELP = f"""\
silica — your personal note curator agent

Usage:
  silica                     open the REPL in the current folder's vault
  silica --gui [--port N]    serve the web GUI (default http://localhost:{GUI_PORT})
  silica doctor [--live]     check the environment (--json for machine output)
  silica init [--advanced]   first-run wizard
  silica connect             bridge to the Obsidian desktop app
  silica import <path>       import external material into the vault
  silica update [--check]    self-update
  silica prune [--apply]     list the indexes of folders that are gone
                             (--apply removes them)
  silica --version           print the version and exit

Options:
  -c "<command>"             run one REPL command and exit (repeatable),
                             e.g. silica -c "/report"
  -v, --verbose              debug logging
  -h, --help                 this help

The vault is the folder you launch in; SILICA_VAULT overrides it. Type /help
inside the REPL for the slash-command reference."""


def _prune_indexes(found: tuple[list[tuple[Path, Path]], list[tuple[Path, Path]]], *, apply: bool) -> int:
    """`silica prune`: list the index dirs whose folder is gone, remove them
    with `apply`. Not only indexes: a dir also keeps that vault's quiz log,
    study offers and change ledger (changes.db), which describe notes gone
    with the folder. A vault that was moved rather than deleted already left
    them behind, since the dir is keyed by path; that is why the listing is
    the default and removal needs `apply` (orphan_indexes)."""
    import shutil
    from rich.markup import escape

    gone, absent = found
    for d, folder in absent:
        CONSOLE.print(f"  kept      [dim]{d.name}[/]  {escape(str(folder))} (its drive is not mounted)")
    if not gone:
        CONSOLE.print("  no index belongs to a folder that is gone")
        return 0
    sizes = [sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) for d, _f in gone]
    for (d, folder), n in zip(gone, sizes):
        CONSOLE.print(f"  {n / 2**20:6.1f} MB [dim]{d.name}[/]  {escape(str(folder))}")
    if not apply:
        CONSOLE.print(f"  {len(gone)} index dirs, {sum(sizes) / 2**20:.1f} MB: "
                      "[bold]silica prune --apply[/] removes them")
        return 0
    freed = failed = 0
    for (d, _folder), n in zip(gone, sizes):
        try:
            shutil.rmtree(d)
            freed += n
        except OSError as e:
            # One dir held open or read-only must not keep the others: it is
            # named here and the exit code says the prune was partial.
            failed += 1
            CONSOLE.print(f"  [yellow]not removed: {escape(str(d))} ({escape(str(e))})[/]")
    CONSOLE.print(f"  removed {len(gone) - failed} index dirs, {freed / 2**20:.1f} MB")
    return 1 if failed else 0


def _dispatch_subcommand(args: list[str]) -> int | None:
    """Handle `silica doctor` / `init` / `connect` / `import` / `update` / `prune`.

    Returns an exit code, or None when no subcommand matched (→ REPL).
    Lazy imports keep REPL startup unchanged. Module attributes (not `from`
    imports) so tests can monkeypatch run_checks / run_wizard / run_connect.
    """
    # --help/--version answer and exit BEFORE any vault/server bootstrap: a
    # first contact typing `silica --help` used to get the full REPL instead,
    # embeddings autostart included.
    if args[:1] in (["--help"], ["-h"], ["help"]):
        print(_CLI_HELP)
        return 0
    if args[:1] in (["--version"], ["-V"], ["version"]):
        try:
            from silica._version import version as _v
        except Exception:
            _v = "unknown"
        print(f"silica {_v}")
        return 0
    if args[:1] == ["update"]:
        import silica.update as update_mod
        return update_mod.update(check_only="--check" in args[1:])
    if args[:1] == ["prune"]:
        from silica.kernel.recall.vault_registry import orphan_indexes
        return _prune_indexes(orphan_indexes(), apply="--apply" in args[1:])
    if args[:1] == ["import"]:
        # Dispatch runs before main()'s setup (like connect below) — the
        # vault has to be selected here or CONFIG.vault_path is whatever the
        # config file last held, and the envelopes land in another vault's
        # inbox (inbox_dir_for keys by vault digest) where /nucleate never
        # drains them.
        _activate_repo_mode()
        import silica.capture as capture_mod
        target = next(iter(positional(args[1:])), "")
        vault = CONFIG.vault_path.strip()
        if not target or not vault:
            CONSOLE.print(
                "  usage: [bold]silica import <export.zip|conversations.json|"
                "~/.claude/projects>[/] (needs a configured vault)")
            return 1
        try:
            created, skipped = capture_mod.run_import(target, vault)
        except (OSError, ValueError) as exc:
            CONSOLE.print(f"  [yellow]import failed: {exc}[/]")
            return 1
        CONSOLE.print(
            f"  imported [bold]{created}[/] conversation(s), skipped {skipped} "
            f"— run [bold]/nucleate[/] to distill them (10 per run)")
        return 0
    if args[:1] == ["doctor"]:
        import silica.onboarding.checks as checks
        as_json = "--json" in args[1:]
        # Under --json stdout IS the payload, so the autostart's and the live
        # probe's console chatter has to land somewhere else or the output
        # stops parsing. CONSOLE resolves sys.stdout per write, so the redirect
        # reaches it.
        quiet = redirect_stdout(sys.stderr) if as_json else nullcontext()
        # The vault startup would open here, chosen the way _activate_repo_mode
        # chooses it minus its writes (vault.yaml, .silicaignore): a check is
        # not an adoption. Left unset, every vault-scoped row read an empty
        # store: a vault with 22 notes embedded said "no retrieval index yet"
        # (2026-10-02).
        from silica.config import VAULT_PINNED
        target = (None if VAULT_PINNED else resolve_cwd_vault(Path.cwd())) or CONFIG.vault_path.strip()
        if target:
            picked = resolve_vault_switch(target)
            if not picked.error and not picked.created:
                CONFIG.vault_path = picked.vault
        with quiet:
            _ensure_servers()  # report the state after the autostart, not before it
            results = checks.run_checks(CONFIG)
            # --live: opt-in real completion (costs a token on hosted providers).
            # A row in BOTH outcomes, appended before rendering: the table has
            # to show what the exit code is about, and a --json consumer has to
            # be able to tell "--live passed" from "--live never ran".
            if "--live" in args[1:]:
                live_ok = _doctor_live_probe()
                results.append(checks.CheckResult(
                    "live probe", "ok" if live_ok else "fail",
                    "the model replied" if live_ok else "the model did not reply"))
            if not as_json:
                checks.render_report(results)
        if as_json:
            print(json.dumps(checks.report_payload(results), ensure_ascii=False, indent=2))
        return checks.exit_code(results)
    if args[:1] == ["init"]:
        import silica.onboarding.wizard as wizard_mod
        return wizard_mod.run_wizard(advanced="--advanced" in args[1:])
    if args[:1] == ["connect"]:
        # Dispatch runs before main()'s setup (unlike --gui) — do it here.
        _activate_repo_mode()
        _announce_code_lane()
        from silica.kernel.base.manifest import apply_manifest_to_config
        apply_manifest_to_config()
        meter.resolve_context_budget()
        _setup_logging(debug="--verbose" in sys.argv or "-v" in sys.argv or CONFIG.debug_logging)
        _ensure_servers()
        import silica.ui.connect as connect_mod
        return connect_mod.run_connect()
    return None


def _start_reminder_daemon() -> None:
    """60 s tick: due event reminders -> banner above the prompt.

    Daemon thread, interactive REPL only. The tick re-resolves the active
    vault every iteration (the vault singleton is first-caller-wins and
    /vault can switch it — a Path captured at thread start would tick
    against the wrong vault). Sleep-first, so the first post-launch tick is
    also the cold-start delivery of reminders missed while the app was
    closed (collapsed to one late notice per event by the kernel).
    Best-effort: a reminder is a courtesy, never a crash.
    """
    import threading
    import time as _time

    def _tick_loop() -> None:
        import datetime as _dt
        from pathlib import Path as _Path

        from rich.markup import escape

        while True:
            _time.sleep(60)
            try:
                raw = (CONFIG.vault_path or "").strip()
                if not raw:
                    continue
                vault = _Path(raw)
                from silica.kernel.calendar.model import scan_events
                from silica.kernel.calendar.reminders import deliver_due
                events = scan_events(vault)
                if not events:
                    continue
                for r in deliver_due(vault, events, _dt.datetime.now()):
                    tag = "[yellow]late reminder[/]" if r["late"] else "[bold cyan]reminder[/]"
                    when = r["start"].strftime("%Y-%m-%d %H:%M")
                    CONSOLE.print(f"  {tag} · {escape(r['title'])} · {when}")
            except Exception as e:
                logger.debug("reminder tick failed (non-fatal): %s", e)

    threading.Thread(target=_tick_loop, daemon=True, name="silica-reminders").start()


def _gui_port() -> int:
    """Parse `--port N` / `--port=N` from argv (default GUI_PORT)."""
    for i, a in enumerate(sys.argv):
        raw = a.split("=", 1)[1] if a.startswith("--port=") else (
            sys.argv[i + 1] if a == "--port" and i + 1 < len(sys.argv) else None
        )
        if raw is not None:
            try:
                return int(raw)
            except ValueError:
                pass
    return GUI_PORT


def main():
    """Entry point for the `silica` CLI command."""
    _args = [a for a in sys.argv[1:] if a not in ("--verbose", "-v")]
    code = _dispatch_subcommand(_args)
    if code is not None:
        sys.exit(code)
    _activate_repo_mode()
    _announce_code_lane()
    from silica.kernel.base.manifest import apply_manifest_to_config
    apply_manifest_to_config()
    meter.resolve_context_budget()
    debug_mode = "--verbose" in sys.argv or "-v" in sys.argv or CONFIG.debug_logging
    _setup_logging(debug=debug_mode)
    _ensure_servers()

    # --gui: serve the localhost web GUI instead of the REPL (config/model/logging
    # already applied above). Blocks on uvicorn until Ctrl-C. Needs the [gui] extra.
    if "--gui" in sys.argv:
        try:
            from silica.ui.web import serve
        except ImportError as exc:
            # server.py imports far more than the [gui] extra (the whole Learn
            # lane, fsrs included), so name the module that actually failed and
            # only sell the extra when it owns it. Blaming [gui] unconditionally
            # sent an editable tool env, stale on a NEW BASE dependency, chasing
            # an extra it already had -- 2026-09-07, fsrs.
            missing = getattr(exc, "name", None) or "?"
            gui_deps = {"fastapi", "uvicorn", "multipart", "mdit_py_plugins", "latex2mathml"}
            if missing.split(".")[0] in gui_deps:
                CONSOLE.print("  [red]The GUI needs an extra:[/] pip install 'silica\\[gui]'")
            else:
                CONSOLE.print(f"  [red]The GUI failed to import[/] {missing}: {exc}")
                CONSOLE.print("  [dim]An editable install goes stale on a new dependency:"
                              " uv tool install -e '.\\[all]' --force[/]")
            sys.exit(1)
        serve(port=_gui_port())
        return

    # Obsidian bridge: host the rpc channel so the plugin can dial in and the
    # driver hot-swaps to ws (writes land through Obsidian's vault API while
    # the app is open). Silent no-op without [connect] or .obsidian/.
    from silica.ui.connect import start_bridge_thread
    _bridge = start_bridge_thread()

    # Wizard first: it prints its own banner and re-execs on success, so running
    # it after print_home() showed the banner twice in one screen. One-shot -c
    # runs skip the banner and the wizard: they are scripting, not a session.
    if "-c" not in sys.argv:
        _autolaunch_wizard_if_unconfigured()  # re-execs on success; returns otherwise
        print_home()
        if _bridge is not None:
            CONSOLE.print(f"  [dim]Obsidian bridge on ws://127.0.0.1:{_bridge.port}[/]\n")
    if not _model_configured():
        CONSOLE.print(_NO_MODEL_HINT)

    # One-shot mode: `silica -c "/report"` runs each -c command through the
    # exact same turn body as the REPL, then exits — scripting without piping
    # a here-doc into an interactive prompt. Repeatable (-c A -c B runs both).
    oneshot: list[str] = [
        sys.argv[i + 1]
        for i, a in enumerate(sys.argv)
        if a == "-c" and i + 1 < len(sys.argv)
    ]
    oneshot_iter = iter(oneshot)

    session = build_session() if not oneshot else None
    if not oneshot:
        _start_reminder_daemon()
        # MCP servers open in the background (ADR-0091); never waited on here.
        from silica.connectors.mcp_tools import start as _start_connectors
        _start_connectors()
    # /incognito is the user memory module's flag (_dc_incognito), read below
    # for capture too, so one toggle covers both. A session starts on the record.
    from silica.kernel import user_memory
    user_memory.set_incognito(False)
    # This session's own identity, for the capture lane. Random, not a clock:
    # two silica processes started in the same second share the same vault WAL,
    # and a deterministic name would have one overwrite the other's envelope.
    # capture_session is opt-in and fail-open in itself, so both call sites
    # below are the bare call — no wrapper, nothing to guard.
    from silica.capture import capture_session
    conv = Conversation(messages=seed.fresh_messages(), sid=uuid.uuid4().hex[:12])
    messages = conv.messages

    from silica.ui.renderer import make_progress_callback
    callback = make_progress_callback()
    # This surface owns the terminal, so here and only here its tty answers
    # whether a person can be asked: piped input has nobody to type a reply.
    ask = Ask(CONSOLE.input, _line, _hidden, _yes) if sys.stdin.isatty() else None

    while True:
        if oneshot:
            nxt = next(oneshot_iter, None)
            if nxt is None:
                break
            user_input = nxt
            CONSOLE.print(f"  [dim]› {user_input}[/]")
        else:
            try:
                # raw=True: background-thread logs (bridge connect, workers) write
                # pre-rendered ANSI to the patched stderr. Without raw, prompt_toolkit
                # escapes the codes and they print literally (e.g. "?[1;2m") above the
                # prompt instead of rendering as colour.
                with patch_stdout(raw=True):
                    user_input = session.prompt(prompt_text(), bottom_toolbar=bottom_toolbar)
            except (EOFError, KeyboardInterrupt):
                print("\n  (_  _)。˚")
                break

        user_input = user_input.strip()
        if not user_input:
            continue
        typed = user_input   # the expansions below rewrite user_input; the named notes are read from this

        worn = session_turn.wear(user_input, conv)
        if worn is not None:
            CONSOLE.print(f"  {worn[0]}")
            if not worn[1]:
                continue
            user_input = typed = worn[1]

        # A command either does the whole job or hands the model a directive
        is_directive = False
        if user_input.startswith("/"):
            try:
                result = _command(user_input, messages, ask=ask, renderer=callback)
            except KeyboardInterrupt:
                # /nucleate drives the whole FSM inline on this thread, and /embed or
                # /wiki run as long. Without this the Ctrl+C escapes main() and kills
                # the REPL with a raw traceback, past the session's capture: the
                # __main__ guard at the bottom never runs, since the installed entry
                # point is the `silica = silica.cli:main` console script.
                # Not "use /revert": an interrupted run that committed nothing has no
                # journalled inverses, so last_active_run() would walk back to an
                # EARLIER run and undo that one instead.
                CONSOLE.print("\n  [dim](interrupted — chunks that already committed stay in the vault)[/]")
                continue
            if result is not None:
                if result.directive is None:
                    continue
                user_input = result.directive
                is_directive = True

        # /web — the consent turn: a normal agent turn with web-only tools and
        # citations built from the tool trace. Checked before the slash handler,
        # since it rewrites the input into an ordinary agent instruction.
        web: tuple[str, str] | None = None
        try:
            web = session_turn.expand_web_turn(user_input, messages)
        except ValueError as e:
            CONSOLE.print(f"  [yellow]{e}[/]")
            continue
        if web is not None:
            user_input = web[1]
            is_directive = True

        # Handle slash commands
        if user_input.startswith("/"):
            cmd = user_input.strip().lower()
            if cmd in ("/clear", "/new"):
                # Before the wipe: /clear destroys this conversation, so the
                # session's own end envelope will not contain it.
                if not user_memory.incognito():
                    capture_session(messages, session_id=conv.sid, driver="tui",
                                    event="session_clear")
                CONSOLE.clear()
                print_home()
                messages[:] = seed.fresh_messages()
                conv.collapsed = set()  # indices reset with the history
                conv.seen_numbers = set()
                from silica.obs import narration as _narr
                _narr.NARRATOR.close()          # next user turn opens a fresh sid
                conv.sid = uuid.uuid4().hex[:12]
                continue

            result = _handle_slash_command(user_input, messages)
            if cmd.startswith("/resume"):
                # From the narration the history came back from. A /resume that
                # failed left the narrator on this session, whose narration holds
                # what the live set does.
                from silica.obs import narration as _narr

                conv.seen_numbers = numbers_of_session(_narr.NARRATOR.sid)
            if result is False:
                print("  (_  _)。˚")
                break
            if result is True:
                continue
            # None → unrecognized command: let the agent infer the intent
            # (ponytail: unknown slash → one LLM round-trip, not a hard reject).
            user_input = (
                f"The user typed the command {user_input!r}, which has no built-in "
                "handler. Interpret their intent from it and use your tools to carry "
                "it out; if it's genuinely unclear, ask one brief clarifying question."
            )
            is_directive = True
            # fall through to the agentic loop below

        # Fail-fast guard: a chat turn without a model would only surface a
        # provider stack trace — point at `silica init` instead.
        if not _model_configured():
            CONSOLE.print(_NO_MODEL_HINT)
            continue

        t = session_turn.Turn(conv, typed, content=user_input, web=web, origin="cli" if is_directive else None)
        for step in _BEFORE:
            step(t)
        # A normal message goes to the agentic loop. CLI-expanded shortcuts carry
        # an `origin` so the wire boundary (and our own bookkeeping) can tell a
        # harness directive apart from a human turn.
        msg = session_turn.user_message(t)
        messages.append(msg)

        # Born at the first user turn (spec §5); idempotent afterwards. The
        # sid is capture's session_id so the two records share one id space.
        from silica.obs import narration as _narr
        _narr.NARRATOR.ensure_session(driver="tui", sid=conv.sid)
        conv.sid = _narr.NARRATOR.sid or conv.sid   # /resume may have switched it
        _narr.NARRATOR.turn(msg)

        _agent_turn(t, callback)

    # Every exit from the REPL passes here: /exit, Ctrl+D, Ctrl+C at the prompt.
    if not user_memory.incognito():
        capture_session(messages, session_id=conv.sid, driver="tui")
    from silica.obs import narration as _narr
    _narr.NARRATOR.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # A second Ctrl+C landing inside the REPL's own interrupt cleanup
        # (or during exception printing) can otherwise escape main() uncaught,
        # hitting interpreter shutdown while abandoned daemon threads (distill
        # prefetch, run_with_deadline) are still alive — CPython then fails to
        # print the traceback (stderr already torn down) and dumps a raw
        # _PyObject_Dump instead. sys.exit() skips traceback printing entirely.
        sys.exit(130)
