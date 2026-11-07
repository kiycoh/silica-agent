# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Silica CLI — the entry point REPL.

From SILICA.md §8.4:
  After `uv pip install -e .`, the command `silica` is in PATH.
  Opens a REPL with prompt_toolkit, runs the agentic loop.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import shlex
import signal
from pathlib import Path
import sys
import threading
import uuid
from collections.abc import Callable
from contextlib import nullcontext, redirect_stdout
from typing import NamedTuple

from silica.term.style import FlatMarkdown

from silica.agent.constraints import AgentConstraints, chat_deferred, chat_tools, web_turn_constraints
from silica.agent.loop import run_agent
from silica.agent import turn_notes
from silica.agent.recall_watch import (
    RecallWatch,
    numbers_notes,
    numbers_of_session,
    unfounded_in_turn,
)
from silica.config import claim_env, CONFIG, env_raw, GUI_PORT, live_env
from silica.onboarding.adopt import resolve_cwd_vault
from silica.prompts import _lang_rule, system_prompt
from silica.sources import ingest
from silica.term.console import CONSOLE
from silica.ui.home import print_home
from silica.ui.prompt import build_session, bottom_toolbar, prompt_text
from prompt_toolkit.patch_stdout import patch_stdout

from silica.tools import catalog
from silica.sources.web_research import WebTurn

catalog.load()

logger = logging.getLogger(__name__)


@functools.cache
def _cl100k():
    """tiktoken's cl100k_base, read from the copy litellm ships, never fetched.

    litellm points TIKTOKEN_CACHE_DIR at that copy when it is imported, and the
    meter now runs before it is (ADR-0073). Without the variable tiktoken
    downloads the file: offline that is a ProxyError, online a copy in /tmp that
    a reboot deletes. find_spec locates the package without importing it.
    """
    import importlib.util

    spec = importlib.util.find_spec("litellm")
    if spec and spec.submodule_search_locations:
        bundled = Path(spec.submodule_search_locations[0]) / "litellm_core_utils" / "tokenizers"
        if bundled.is_dir():
            os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(bundled))
    import tiktoken

    return tiktoken.get_encoding("cl100k_base")


# What the chat template wraps around each message (`<|im_start|>role\n` ...
# `<|im_end|>\n` on Qwen); OpenAI's own envelope is 3.
_TOKENS_PER_MESSAGE = 4


def _count_context_tokens(messages: list[dict], tools: list[dict] | None = None) -> int:
    """Pure counter — lets callers (e.g. the web seed prewarm) count a candidate
    message list without clobbering the live session's CONFIG.context_tokens.

    cl100k over each message's text and tool calls, plus the tool block as JSON,
    which is how the Jinja chat templates of the local routes render it. Not
    litellm.token_counter: against llama-server's own prompt_tokens on Qwen3.5-9B
    (2026-10-07) it was 23% short on the seed and 9 to 12% short on the largest
    sessions, against 2% and 4 to 8% here (ADR-0073), and importing litellm to
    count cost the GUI 161 MB at startup. A sum, so parts add up to the total.
    """
    # Counted on the WIRE form: the kept thinking trace would otherwise show up
    # in the meter as context the provider is charged for, when `to_wire` drops
    # it before the request.
    from silica.model.limits import ROUGH_CHARS_PER_TOKEN
    from silica.model.wire import text_of, to_wire

    messages = [to_wire(m) for m in messages]
    try:
        enc = _cl100k()

        def n(text: str) -> int:
            # disallowed_special=(): a note that quotes "<|endoftext|>" is text to
            # count, and tiktoken's default raises on it, which sent the whole
            # window to the fallback below.
            return len(enc.encode(text, disallowed_special=()))

        total = sum(
            n(text_of(m.get("content"))
              + (json.dumps(m["tool_calls"]) if m.get("tool_calls") else ""))
            + _TOKENS_PER_MESSAGE
            for m in messages
        )
        return total + (n(json.dumps(tools)) if tools else 0)
    except Exception:
        # No encoding (a broken tiktoken install): a rough meter beats none.
        return sum(len(text_of(m.get("content"))) for m in messages) // ROUGH_CHARS_PER_TOKEN


def _chat_tool_schemas(messages: list[dict]) -> list[dict]:
    """The tool block every interactive turn ships alongside the history.

    Counted because it is *sent*: 51 schemas measured 7.2k tokens on 2026-09-02
    against 2.1k of seed messages, so a meter that totalled the messages alone
    read 2.1k while the provider billed 12.7k for the same turn — the ring was
    reporting a sixth of the window it was there to guard. `chat_tools` is the
    same selector the REPL and the GUI hand to AgentConstraints; a batch run
    (nucleate, injector) ships a different set, but the meter is a chat surface
    and never shows one.
    """
    from silica.agent.constraints import chat_deferred, chat_tools
    from silica.tools import TOOLS

    names = chat_tools(messages)
    # the reach the loop cuts a schema's pointers by, or the meter counts text never sent
    reach = {*names, *chat_deferred(messages)}
    return [TOOLS[n].json_schema(reach) for n in names if n in TOOLS]


def _update_context_tokens(messages: list[dict]) -> None:
    CONFIG.context_tokens = _count_context_tokens(messages, _chat_tool_schemas(messages))


# What put a message in the window. `tool_io` takes the assistant turns that
# carry tool_calls as well as the results: the arguments are what the model paid
# to emit, and an assistant message holding a call carries no prose worth billing
# to the conversation.
def _context_group(m: dict) -> str:
    role = m.get("role")
    if role == "system":
        return "system"
    if role == "tool" or (role == "assistant" and m.get("tool_calls")):
        return "tool_io"
    return "messages"


def _context_breakdown(messages: list[dict]) -> dict[str, int]:
    """The same window `_count_context_tokens` totals, split by what filled it.

    The counter is a sum over messages plus the tool block, so the parts add up
    to the total beside them by construction. The tool block is its own part
    rather than folded into the system prompt: it is the single biggest resident
    of an idle window (7.2k against 2.1k of instructions), and a part that large
    hidden inside another is the reading the panel exists to prevent.
    """
    groups: dict[str, list[dict]] = {"system": [], "tool_io": [], "messages": []}
    for m in messages:
        groups[_context_group(m)].append(m)
    counts = {k: (_count_context_tokens(v) if v else 0) for k, v in groups.items()}
    schemas = _chat_tool_schemas(messages)
    counts["tool_specs"] = _count_context_tokens([], schemas) if schemas else 0
    return counts


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


def _compact_context(messages: list[dict], collapsed: set[int]) -> set[int]:
    """Collapse old read-tool results once the context meter crosses the budget.

    The between-turns sweep; the agent loop runs the same pass per iteration
    (see run_agent). Runs after _update_context_tokens (which feeds
    prompt_tokens); when anything collapsed, recounts so the toolbar meter
    reflects the slimmer history. Loss is recoverable: each stub names the call
    to re-issue.
    """
    from silica.agent.compaction import (
        COMPACT_FLOOR_TURNS,
        COMPACT_FRACTION,
        compact_read_history,
    )
    from silica.tools import TOOLS

    updated = compact_read_history(
        messages,
        collapsed,
        prompt_tokens=CONFIG.context_tokens,
        budget=int(COMPACT_FRACTION * CONFIG.max_context_tokens),
        floor_turns=COMPACT_FLOOR_TURNS,
        tools=TOOLS,
    )
    if updated != collapsed:
        _update_context_tokens(messages)
    return updated


def _inject_vault_map(messages: list[dict]) -> None:
    """Appends the vault map as a system message (best-effort).

    CoALA recall: loads the corpus self-model into working memory at session
    start so the agent doesn't rediscover the vault via tools. The map is a
    startup snapshot; this session's writes already live in working memory.
    # recomputed once per session; no storage/refresh.
    """
    try:
        from silica.kernel.report.vault_map import build_vault_map

        vault_map = build_vault_map()
        if vault_map:
            messages.append({"role": "system", "content": vault_map})
    except Exception as exc:
        logger.debug("vault map injection skipped: %s", exc)


def _today_line() -> str:
    """Today's date, which the model cannot get from anywhere else in the seed.

    `silica_event_create` takes an absolute 'YYYY-MM-DD HH:MM' and `silica_agenda`
    only 'today' or an ISO date, so resolving "next Wednesday" is the model's job
    — and without this it resolves it against its training cutoff. The one other
    date in the context is incidental: the run-log tail inside the vault map,
    which is the *last run's* day and is a best-effort block that can be absent.

    Session-scoped, like the rest of the seed: a session that spans midnight
    keeps the day it started on. Per-turn re-stamping was declined 2026-08-19.
    """
    import datetime as _dt

    today = _dt.date.today()
    return (
        f"Today is {today:%A, %d %B %Y} ({today.isoformat()}). Resolve relative "
        f'dates ("tomorrow", "next Wednesday") against it, never against anything '
        f"you remember."
    )


def _vault_scope() -> str:
    """One line naming the two paths the agent must not confuse.

    Reads span the whole vault; new notes are confined to `write_dir`. Without
    this the model reads "vault" as the folder it writes in and reports an empty
    vault while sitting on a repo full of Markdown.
    """
    from silica.kernel.base.manifest import active_write_dir
    from silica.kernel.base.manifest import SAFE_WRITE_DIR

    vault = CONFIG.vault_path
    write_dir = active_write_dir()
    if not write_dir:
        return f"Vault: {vault} — you read and write notes anywhere under it."
    scope = (
        f"Vault: {vault} — you read everything under it, including files that "
        f"are not yours (a repo's own README, docs, specs). New notes go under "
        f"{write_dir}/, the only place you may write; that folder being empty "
        f"does not mean the vault is empty."
    )
    if write_dir != SAFE_WRITE_DIR:
        return scope
    # The mirror only earns its merge-by-paste if the model replicates the tree
    # it can already see in the vault map, so the instruction ships with it.
    return scope + (
        f" {write_dir}/ is a staging mirror of the vault's own tree: a note that "
        f"belongs in Projects/ goes to {write_dir}/Projects/, so the user merges "
        f"by pasting the folder's contents over the vault. Reuse the folders the "
        f"vault already has. A folder it does not have yet is rejected unless the "
        f"op carries a \"reason\" stating why no existing folder fits."
    )


def seed_messages(math: bool = False, app: bool = False) -> list[dict]:
    """The system context a fresh conversation starts from: prompt, date, vault
    scope, vault map, and the restated language rule (`_reply_language`).

    Shared by the TUI and the GUI (`ui/web/server._build_seed`, which passes
    math=True for the MathML renderer and app=True for the help panel the chat
    answers from, ADR-0115). It used to be TUI-only, and the GUI's
    own seed had drifted to prompt + map: no `_vault_scope`, so the model was
    blind to `write_dir`, and no closing language line, which is the fix commit
    93b6d2d shipped here and never carried across. One builder, no drift.
    """
    reply = _reply_language()
    messages: list[dict] = [{"role": "system", "content": system_prompt(reply, math=math)}]
    messages.append({"role": "system", "content": _today_line()})
    messages.append({"role": "system", "content": _vault_scope()})
    if app:
        # Before the map, which is the seed's bulk. On 2026-10-16 the seed with
        # it answered 13 to 15 of 14 to 15 how-to questions a run, against 4 to 5
        # without (docs evaluation/app-knowledge-2026-10-16).
        from silica.ui.web.app_help import app_message
        messages.append({"role": "system", "content": app_message()})
    _inject_vault_map(messages)
    # User memory (ADR-0076), frozen for the session: a save lands on disk and
    # in its tool result and shows here from the next session, so the model's
    # picture of the user does not move under it mid-conversation. After the
    # map and before the language line, which must stay last.
    from silica.kernel import user_memory

    if (memory := user_memory.snapshot_message()) is not None:
        messages.append(memory)
    # The vault's author's standing instructions (ADR-0127 point 4), frozen
    # for the session as memory is, in the same place for the same reason.
    from silica.kernel import vault_instructions

    if (told := vault_instructions.message(CONFIG.vault_path or None)) is not None:
        messages.append(told)
    # The vault map is the vault's own language. On a vault whose notes are not
    # in `reply`, that bulk drowns the language rule sitting in message 0, and
    # the model answers in the notes' language. Restate it last, closest to the
    # user turn.
    messages.append({"role": "system", "content": _lang_rule(reply)})
    return messages


def _reply_language() -> str:
    """The chat language the vault declares, "" when it declares none.

    Not the language its notes are detected in, which this also returned until
    2026-10-16: that set "Reply in italian by default" on every undeclared
    Italian vault, and 10 of 12 Chinese questions got an Italian answer. A slash
    command, which has no language of its own, is the follow rule's to place.
    """
    if not CONFIG.vault_path:
        return ""
    from silica.kernel.base.manifest import load_manifest

    conv = load_manifest(CONFIG.vault_path).conventions
    return conv.reply_language or conv.language or ""


def _fresh_messages() -> list[dict]:
    """`seed_messages` plus the live context meter — session start and /clear.

    The meter update is what the GUI must NOT do off the request path (it would
    clobber the meter of the conversation in progress), so it lives here rather
    than in the shared builder.
    """
    messages = seed_messages()
    _update_context_tokens(messages)
    return messages


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


class VaultTarget(NamedTuple):
    """Outcome of resolving a runtime ``/vault <arg>`` switch.

    ``vault`` is the absolute path to adopt, ``created`` True when the directory
    does not exist yet and the caller must mkdir it. ``error`` is set (and the
    other fields meaningless) only when the path cannot be a vault at all.
    """
    vault: str
    created: bool
    error: str | None = None


def resolve_vault_switch(arg: str) -> VaultTarget:
    """Resolve a ``/vault <arg>`` (or explicit ``SILICA_VAULT``) target.

    The path is adopted **as-is**, always: the vault is the folder the user
    named, never a subfolder Silica invents or remembers. Whether notes may be
    written into that root or into a subtree is a separate axis, declared
    per-vault as `write_dir` in ``vault.yaml`` (see `onboarding.adopt`).

    So a vault created before that split reads its whole repo again on the next
    launch, while its notes stay in the ``docs/silica`` its manifest now names.
    Read-only I/O.
    """
    from pathlib import Path

    target = Path(arg).expanduser().resolve()
    if target.exists() and not target.is_dir():
        return VaultTarget("", False, f"not a directory: {target}")
    return VaultTarget(str(target), not target.is_dir())


class VaultSwitch(NamedTuple):
    """What a completed ``switch_vault`` did, for whoever has to report it.

    ``error`` set means nothing happened at all. Everything else is a thing the
    caller may want to say out loud: ``created`` the directory did not exist,
    ``write_dir`` this call declared one in vault.yaml, ``invalid_write_dir``
    the manifest declares one Silica cannot resolve (every write will be
    rejected), ``repo_warning`` the vault⊂repo invariant is violated,
    ``language_drift`` the co-occurrence store is frozen in another language.
    """
    vault: str
    error: str | None = None
    created: bool = False
    write_dir: str | None = None
    seeded_ignore: bool = False
    invalid_write_dir: bool = False
    repo_warning: str = ""
    language: str = ""
    store_language: str = ""
    language_drift: bool = False


def switch_vault(arg: str) -> VaultSwitch:
    """Point the running process at another vault — the whole sequence.

    Not an assignment: a live process holds a driver, an overlay cache, a
    manifest and vault-scoped index caches, and every one of them still answers
    for the old folder until it is reset. Skipping any single step leaves reads
    and writes disagreeing about which vault they are in.

    Prints nothing, so the REPL and the web settings panel can share it: the
    caller renders the returned record. Never raises for a bad path — that is
    ``error``.
    """
    from pathlib import Path

    from silica.driver import reset_driver
    from silica.kernel.base.roots import repo_root_warning
    from silica.kernel.recall.relatedness import reset_vault_caches
    from silica.kernel.text.overlay import reset_overlay_cache
    from silica.kernel.base.manifest import (
        apply_manifest_to_config,
        get_active_manifest,
        reset_manifest_cache,
    )
    from silica.onboarding.adopt import declare_write_dir, seed_silicaignore
    from silica.kernel.vault_language import language_status

    target = resolve_vault_switch(arg)
    if target.error:
        return VaultSwitch("", error=target.error)
    resolved = target.vault
    if target.created:
        Path(resolved).mkdir(parents=True, exist_ok=True)
    declared = declare_write_dir(resolved)
    seeded = seed_silicaignore(resolved)
    CONFIG.vault_path = resolved
    reset_driver()
    reset_overlay_cache()  # overlay is vault-scoped; don't serve the old vault's
    reset_manifest_cache()  # manifest is vault-scoped too
    apply_manifest_to_config()
    # Vault-scoped store caches are path-keyed (harmless on lookup) but retain
    # the old vault's index/vectors for the process lifetime.
    reset_vault_caches()
    lang, store_lang, drift = language_status(resolved)
    return VaultSwitch(
        vault=resolved,
        created=target.created,
        write_dir=declared,
        seeded_ignore=bool(seeded),
        # Declared but unresolvable (absolute/traversal). Refusing here is the
        # whole point of not degrading it to "" in the parser.
        invalid_write_dir=get_active_manifest().write_dir is None,
        repo_warning=repo_root_warning(resolved) or "",
        language=lang or "",
        store_language=store_lang or "",
        language_drift=bool(drift),
    )


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


def _direct_args(raw: str) -> list[str]:
    """A direct command's words, a double-quoted run being one word, so
    `/check "Drafts/Opinion on limitation"` names the note and not a note
    called `"Drafts/Opinion`. Not shlex's default: an apostrophe is a letter in
    prose (`/find l'entropia`), `#` starts a tag and not a comment, and a
    backslash is a Windows path's. An unbalanced quote leaves the words as
    typed, as every direct command read them before quotes were honoured."""
    lex = shlex.shlex(raw, posix=True)
    lex.whitespace_split, lex.quotes, lex.escape, lex.commenters = True, '"', "", ""
    try:
        return list(lex)
    except ValueError:
        return raw.split()


def _unquoted(text: str) -> str:
    """The path a command takes whole (`/undo`, `/delete`, `/trash restore`)
    without one pair of quotes around it: those read the raw line, since a
    note's name has spaces, and a quoted name was looked up with its quotes."""
    text = text.strip()
    return text[1:-1].strip() if len(text) > 1 and text[0] == text[-1] and text[0] in "\"'" else text


def _positional(args: list[str]) -> list[str]:
    """The tokens that are not flags — anything not starting with `-`.

    Slash commands take their subject positionally and their options as
    `--key=value`, so this and `_str_flag` are the whole parser. argparse would
    own sys.argv and exit the REPL on a typo; these two never do.
    """
    return [a for a in args if not a.startswith("-")]


def _str_flag(args: list[str], flag: str, default: str = "") -> str:
    """`--flag=value` out of args, or `default` when it is absent.

    `flag` is given without the `=` ("--target"); an empty `--target=` yields ""
    and is therefore indistinguishable from absent, which every caller wants.
    """
    prefix = flag + "="
    return next((a[len(prefix):] for a in args if a.startswith(prefix)), default)


def _int_flag(args: list[str], flag: str, default: int) -> int:
    """`--flag=N` out of args; keeps the default when absent or not a number."""
    raw = next((a[len(flag):] for a in args if a.startswith(flag)), None)
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


# The three index refreshes differ only in tool and result label.
_REFRESH = {
    "/embed": ("silica_embed_refresh", ""),
    "/cooccur": ("silica_cooccurrence_refresh", " (co-occurrence)"),
    "/lexical": ("silica_lexical_refresh", " (lexical)"),
}


# /contested scan memo: (epoch, rows) — the scan parses every note's
# frontmatter, so it runs once per file-state epoch.
_CONTESTED_SCAN_MEMO: dict[str, tuple[str, list]] = {}


# --- direct commands ---------------------------------------------------------
# Read-only work the REPL does itself, with no LLM round-trip. Same shape as the
# workflow shortcuts below: one handler per command, a table instead of a chain.
# Each takes the whitespace-split tokens AFTER the command word; `raw_input` (the
# case-preserved line, which is what a query or a path must be read from) is
# passed to all of them and absorbed by `**_`.


def _dc_vault(args: list[str], **_) -> bool:
    """/vault [<path>] — show the active vault, or adopt another one."""
    from silica.driver import driver_kind

    arg = " ".join(args).strip()
    if arg:
        r = switch_vault(arg)
        if r.error:
            CONSOLE.print(f"  [red]Cannot adopt as a vault — {r.error}[/]")
            return True
        if r.created:
            CONSOLE.print(f"  Created [bold]{r.vault}[/] as the session vault.")
        if r.write_dir:
            CONSOLE.print(
                f"  Source tree — writes confined to [bold]{r.write_dir}/[/]; the rest of "
                "the vault is read-only context. Change `write_dir` in vault.yaml."
            )
        if r.seeded_ignore:
            CONSOLE.print("  Created [bold].silicaignore[/] — add folders to keep out of the index.")
        if r.invalid_write_dir:
            CONSOLE.print(
                "  [red]⚠ vault.yaml declares an invalid `write_dir` — every write "
                "will be rejected until it is a relative path inside the vault.[/]"
            )
        CONSOLE.print(f"  Vault → [bold]{r.vault}[/] (backend: {driver_kind()})")
        if r.repo_warning:
            CONSOLE.print(f"  [yellow]⚠ {r.repo_warning}[/]")
        # Surface the frozen-language drift here, not only in `/vault` info:
        # a switch is exactly when a wrong-frozen store (english on an IT
        # vault) would otherwise stay silent. Reuses the doctor's check.
        if r.language_drift:
            CONSOLE.print(
                f"  [yellow]⚠ Language: {r.language}, co-occurrence store "
                f"frozen {r.store_language} — run /cooccur --force to rebuild.[/]"
            )
        CONSOLE.print(
            "  [dim]Index namespace follows the vault — run /embed and /cooccur "
            "if this vault has not been indexed yet.[/]"
        )
        return True
    vault = CONFIG.vault_path or "(not configured)"
    CONSOLE.print(f"  Vault:   [bold]{vault}[/]")
    CONSOLE.print(f"  Backend: {driver_kind()}")
    if CONFIG.vault_path:
        from pathlib import Path

        count = len(list(Path(CONFIG.vault_path).rglob("*.md")))
        CONSOLE.print(f"  Notes:   {count}")
        from silica.kernel.vault_language import language_status

        lang, store_lang, drift = language_status(CONFIG.vault_path)
        if lang and drift:
            CONSOLE.print(
                f"  Language: {lang} (store frozen: {store_lang} "
                "⚠ — run /cooccur --force to rebuild)"
            )
        elif lang and store_lang:
            CONSOLE.print(f"  Language: {lang} (store: {store_lang})")
        elif lang:
            CONSOLE.print(f"  Language: {lang}")
    return True


def _dc_status(args: list[str], **_) -> bool:
    """/status [run_id] — progress of the newest run, or of the one named.

    The bare form now opens with the narration's view of THIS session
    (ticket 12): running spans, context pressure, spend — the same fold the
    surfaces render, so /status cannot disagree with them. The ledger digest
    below it stays: it answers recovery, which narration refuses to carry.
    """
    if not args:
        from silica.obs import narration as _narr
        sid = _narr.NARRATOR.sid
        if sid is not None:
            st = _narr.fold_all(_narr.read_beats(_narr.narration_dir() / f"{sid}.jsonl"))
            running = [sv for sv in st.spans.values() if sv.ended_ts is None]
            CONSOLE.print(f"  session [bold]{sid}[/] · beat {st.cursor}"
                          f" · context {st.context_tokens:,} tok"
                          f" · spent {st.cost_tokens:,} tok out")
            for sv in running[:10]:
                CONSOLE.print(f"    [cyan]{sv.kind}[/] {sv.summary} [dim](running)[/]")
            if st.gaps:
                CONSOLE.print(f"  [yellow]gaps in the record: {st.gaps}[/]")
    from silica.tools import TOOLS

    run_id = args[0] if (len(args) + 1) > 1 else ""
    result = TOOLS["silica_ledger_digest"].run(run_id=run_id)
    try:
        parsed = json.loads(result)
        digest = parsed.get("digest", result)
        # Preformatted plain text: Markdown would reflow every line into one
        # paragraph, and markup would eat the "[16 checkpoints]" brackets.
        CONSOLE.print(str(digest), markup=False, highlight=False)
    except Exception:
        CONSOLE.print(result)
    # E(vault) cache line — written by /report (write_report). No cache
    # file → nothing shown; /status never triggers a VaultReport itself.
    try:
        from pathlib import Path as _EP
        energy_file = _EP(CONFIG.vault_path or "") / ".silica" / "energy.json"
        if energy_file.is_file():
            e = json.loads(energy_file.read_text(encoding="utf-8"))
            line = f"  E(vault): [bold]{e['value']:+.2f}[/]"
            if e.get("prev") is not None:
                line += f"  (delta {e['value'] - e['prev']:+.2f} since last report)"
            CONSOLE.print(line)
            # Attribute the delta: the six contributions sum to the total, so
            # naming the terms that moved says WHICH force changed the vault.
            # Movers only — an unchanged term is noise on this line.
            terms, prev_terms = e.get("terms") or {}, e.get("prev_terms") or {}
            movers = sorted(
                ((t, v - prev_terms[t]) for t, v in terms.items() if t in prev_terms),
                key=lambda kv: -abs(kv[1]),
            )
            movers = [(t, d) for t, d in movers if abs(d) >= 0.01]
            if movers:
                CONSOLE.print(
                    "    moved: " + ", ".join(f"{t} {d:+.2f}" for t, d in movers[:4]),
                    markup=False,
                )
    except Exception:
        pass
    return True


def _dc_refresh(args: list[str], cmd: str) -> bool:
    """/embed|/cooccur|/lexical [folder] [--force] — rebuild one index.

    One body, three commands: `_REFRESH` maps each to its tool and its label.
    """
    from silica.tools import TOOLS

    tool, label = _REFRESH[cmd]
    folder = ""
    for part in args:
        if part.startswith("--folder="):
            folder = part[len("--folder="):]
        elif not part.startswith("-"):
            folder = part
    result = TOOLS[tool].run(folder=folder, force="--force" in args)
    try:
        parsed = json.loads(result)
        if "error" in parsed:
            CONSOLE.print(f"  [red]Error:[/] {parsed['error']}")
        else:
            CONSOLE.print(
                f"  Indexed: [bold]{parsed.get('indexed', '?')}[/] / "
                f"{parsed.get('total_notes', '?')} notes{label}"
            )
        if parsed.get("read_errors"):
            CONSOLE.print(f"  [yellow]Read errors:[/] {parsed['read_errors']}")
    except Exception:
        CONSOLE.print(result)
    return True


def _dc_wiki(args: list[str], **_) -> bool:
    """/wiki [folder] — prose pass over the staged code notes."""
    args = args
    folder = next(iter(_positional(args)), "") or None
    overview_only = "--overview-only" in args
    force = "--force" in args
    from silica.capabilities.codewiki import run_wiki
    result = run_wiki(CONFIG, folder=folder,
                      overview_only=overview_only, force=force)
    if result["status"] == "no_repo":
        CONSOLE.print("  [yellow]wiki: vault is not inside a git repo, nothing to describe.[/]")
    elif result["status"] == "empty":
        CONSOLE.print("  [yellow]wiki: no supported source files found "
                      "(code lane parses Python/TypeScript/JavaScript only).[/]")
    elif result["status"] == "error":
        CONSOLE.print(f"  [yellow]wiki: {result.get('reason', 'error')}[/]")
    else:
        CONSOLE.print(
            f"  wiki: {len(result['written'])} note(s) written, "
            f"{len(result['skipped'])} up-to-date"
            + (f", {result['parse_errors']} file(s) not analyzable" if result["parse_errors"] else "")
        )
        for fail in result.get("failed", []):
            CONSOLE.print(f"  [red]wiki: write failed:[/] {fail['path']}: {fail['reason']}")
    return True


def _dc_graph(args: list[str], **_) -> bool:
    """/graph [output.html] [folder] — export the vault graph and open it."""
    from silica.tools.graph import graph_export

    output_path = "graph.html"
    folder = ""
    positional = _positional(args)
    if positional:
        output_path = positional[0]
    if len(positional) > 1:
        folder = positional[1]
    # The user's own command keeps any path; the model's tool is the one held
    # to the vault.
    result = graph_export(output_path=output_path, folder=folder)
    CONSOLE.print(f"  Graph written to: [bold]{result.get('path', output_path)}[/]")
    return True


def _dc_map(args: list[str], **_) -> bool:
    """/map <note> [--force] — the association field around one note."""
    from silica.tools import TOOLS

    force = "--force" in args
    positional = _positional(args)
    note = " ".join(positional).strip()
    if not note:
        CONSOLE.print("  Usage: /map <note> [--force]")
        return True
    result = TOOLS["silica_mindmap"].run(note_path=note, force=force)
    try:
        parsed = json.loads(result)
        if parsed.get("skipped"):
            CONSOLE.print(
                f"  [yellow]Map already present[/] ({parsed['skipped']}), not "
                "overwritten. Use [bold]/map <note> --force[/] to regenerate."
            )
        elif "error" in parsed:
            CONSOLE.print(f"  [red]{parsed['error']}[/]")
        else:
            CONSOLE.print(
                f"  Map written: [bold]{parsed.get('path', '?')}[/] "
                f"({parsed.get('nodes', '?')} nodes, {parsed.get('edges', '?')} edges)"
            )
    except Exception:
        CONSOLE.print(result)
    return True


def _dc_find(args: list[str], *, raw_input: str = "", **_) -> bool:
    """/find <query> [--k=N] [--min-trust=T] [--lifecycle=L] — semantic search,
    printed in the terminal. The two epistemic flags (spec M2) filter AFTER the
    search, so the printed count says what they cost."""
    k = _int_flag(args, "--k=", 5)
    min_trust = _str_flag(args, "--min-trust").strip().lower()
    life = _str_flag(args, "--lifecycle").strip().lower()
    # original case preserved — raw_input, not the lowered cmd
    query = " ".join(_positional(args))
    if not query:
        CONSOLE.print("  Usage: /find <query> [--k=N] [--min-trust=human|grounded|distilled|kept] "
                      "[--lifecycle=active|review|contested|superseded]")
        return True
    from silica.kernel.recall.search import facade_search

    # The core and not the tool: the tool's payload drops `score` for the
    # model (graph._without_score), and this list shows it to the person.
    result = ""
    try:
        parsed = facade_search(query, k=k)
        result = json.dumps(parsed)
        results = parsed.get("results", [])
        if results and (min_trust or life):
            results, dropped = _epistemic_filter(results, min_trust, life)
            if dropped:
                CONSOLE.print(f"  [dim]{dropped} result(s) filtered by "
                              f"{'--min-trust=' + min_trust if min_trust else ''}"
                              f"{' ' if min_trust and life else ''}"
                              f"{'--lifecycle=' + life if life else ''}[/]")
        if results:
            CONSOLE.print(f"  Results for [bold]{query}[/] (top {len(results)}):")
            for r in results:
                score = r.get("score", 0.0)
                path = r.get("path", r.get("name", "?"))
                CONSOLE.print(f"    [{score:.3f}] {path}")
        elif "error" in parsed:
            CONSOLE.print(f"  [yellow]{parsed['error']}[/]")
        else:
            CONSOLE.print(f"  No results for '{query}'.")
    except Exception:
        CONSOLE.print(result)
    return True


def _epistemic_filter(results: list[dict], min_trust: str, life: str) -> tuple[list[dict], int]:
    """`(kept, dropped)` of search rows under a trust floor and/or a lifecycle,
    read off each note's frontmatter (contested.trust / .lifecycle). A row
    whose note cannot be read is dropped: an unreadable note has no trust."""
    from silica.driver import DRIVER
    from silica.kernel.base import frontmatter
    from silica.kernel.write.contested import TRUST_ORDER, lifecycle, trust

    floor = TRUST_ORDER.get(min_trust, 0)
    kept: list[dict] = []
    for r in results:
        ref = r.get("path") or r.get("name") or ""
        try:
            content = DRIVER.read_note(ref if ref.endswith(".md") else ref + ".md").content or ""
        except Exception:
            continue
        data, _raw, _body = frontmatter.split(content)
        if TRUST_ORDER.get(trust(content), 0) < floor:
            continue
        if life and lifecycle(data) != life:
            continue
        kept.append(r)
    return kept, len(results) - len(kept)


def _quoted_flag(args: list[str], flag: str) -> tuple[str | None, list[str]]:
    """(`--flag value` or `--flag "two words"`, the args without it). A
    double-quoted name arrives whole (`_direct_args`); a single-quoted one
    still arrives in pieces and is joined back here; None when the flag is
    absent, "" when it has no value."""
    for i, a in enumerate(args):
        if a == flag or a.startswith(flag + "="):
            head = a[len(flag) + 1:] if a.startswith(flag + "=") else (args[i + 1] if i + 1 < len(args) else "")
            j = i + 1 if a.startswith(flag + "=") else i + 2
            words = [head]
            if head[:1] in "\"'" and head and not (len(head) > 1 and head.endswith(head[0])):
                while j < len(args) and not words[-1].endswith(head[0]):
                    words.append(args[j])
                    j += 1
            value = " ".join(words).strip().strip("\"'").strip()
            if head.startswith("--"):
                value, j = "", j - 1
            return value, args[:i] + args[j:]
    return None, list(args)


def _dc_verify(args: list[str], **_) -> bool:
    """/verify <note> [--clear] — a person vouches for a note (OKF §5.2), bound
    to the body as it is now; --clear withdraws every human entry. The only
    writer of a `human:` attestation: the write tools refuse the key (spec M4)."""
    import datetime
    import getpass

    from silica.config import hosted
    from silica.driver import DRIVER
    from silica.kernel.write.checkpoints import push_landed
    from silica.kernel.write.notetype import clear_verified, stamp_verified

    # --as "<name>" signs with that name. A stamp never reads or writes a
    # /check's state, nor a check the stamp (ADR-0110 point 1).
    signer, rest = _quoted_flag(args, "--as")
    name = " ".join(_positional(rest))
    if not name or signer == "":
        CONSOLE.print('  Usage: /verify <note> [--clear] [--as "<name>"]')
        return True
    try:
        nc = DRIVER.read_note(name)
    except Exception as e:
        CONSOLE.print(f"  [yellow]Cannot read '{name}': {e}[/]")
        return True
    path, prior = nc.ref.path or name, nc.content or ""
    # On the server the login is the tenant's service user, which names no
    # one: there the stamp says `owner` unless the person signs (ADR-0110).
    login = signer.strip() if signer else ("owner" if hosted() else getpass.getuser())
    who, today = f"human:{login}", datetime.date.today().isoformat()
    try:
        new = clear_verified(prior) if "--clear" in args else stamp_verified(prior, who, today)
    except ValueError as e:
        CONSOLE.print(f"  [yellow]{e}[/]")
        return True
    if new == prior:
        CONSOLE.print(f"  {path}: nothing to change.")
        return True
    from silica.kernel.write.preserve import Loss

    DRIVER.overwrite(path, new, loss=Loss.PERSON)  # the person's own attestation
    push_landed(path, prior, new)  # /undo reaches it; the edited lane knows it
    if "--clear" in args:
        CONSOLE.print(f"  {path}: human attestations cleared.")
    else:
        CONSOLE.print(f"  {path}: verified by {who} at {today}.")
    return True


def _dc_check(args: list[str], **_) -> bool:
    """/check <note> — read a note the person wrote against the vault, block
    by block, and print the findings by grade (ADR-0110): the rules first,
    then Jev's verdict on each block they did not settle. It never edits the
    note."""
    import time

    from silica import metrics
    from silica.config import CONFIG
    from silica.driver import DRIVER
    from silica.kernel.check import run as check
    from silica.kernel.base.manifest import active_write_dir

    name = " ".join(_positional(args))
    if not name:
        CONSOLE.print("  Usage: /check <note>")
        return True
    try:
        path = DRIVER.read_note(name).ref.path or name
    except Exception as e:
        CONSOLE.print(f"  [yellow]Cannot read '{name}': {e}[/]")
        return True
    path = path if path.endswith(".md") else path + ".md"
    started = time.monotonic()
    search, dense, floor = _check_legs()
    try:
        report = check.check_note(path, CONFIG.vault_path, write_dir=active_write_dir(),
                                  search=search, dense=dense, dense_floor=floor, judge=_check_judge(cite=False))
    except Exception as e:
        metrics.emit("check", outcome=metrics.outcome_of(e), ms=metrics.ms_since(started))
        CONSOLE.print(f"  [yellow]The check failed: {e}[/]")
        return True
    c = report["counts"]
    metrics.emit("check", outcome="ok", ms=metrics.ms_since(started), blocks=report["blocks"], cached=report["cached"],
                 error=c["error"], warning=c["warning"], nit=c["nit"])
    CONSOLE.print(check.render(report), markup=False, highlight=False)
    return True


def _check_judge(cite: bool = True):
    """Jev decides a block's verdict; the chat model answers only when Jev
    answers nothing (ADR-0110 point 7, the owner's decision of 2026-10-24)."""
    from silica.model.decide import decide
    from silica.model.providers import get_provider
    from silica.config import CONFIG
    from silica.kernel.check import judge as jd

    def chat(prompt: str) -> str:
        # reasoning off: a thinking pass spends the budget before the JSON
        # (llm.py, the 9B's empty answers of 2026-10-15).
        resp = get_provider(CONFIG).call_llm(messages=[{"role": "user", "content": prompt}],
                                             max_tokens=300, temperature=0.0, reasoning=False)
        return resp.text or ""

    return lambda text, passages: jd.judge(text, passages, decide=decide, chat=chat, cite=cite)


def _check_web_legs():
    """What a selection's web check reads with (ADR-0110, amendment of
    2026-11-06): the lanes and fetch_page, neither of which touches a chat
    turn's web state."""
    from silica.sources.web_fetch import fetch_page
    from silica.sources.web_research import search_lanes

    return search_lanes, fetch_page


def _check_legs():
    """The passage search and the dense leg a /check reads with. The dense leg
    is the vault's own embed index; a vault without one, an embedder that is
    down, or an embedder with no measured floor gives None, and absence then
    waits for the model rather than be decided on words alone (ADR-0110
    point 6: an Italian block over English sources)."""
    from silica.config import CONFIG
    from silica.kernel.check import run as check
    from silica.kernel.recall.engine import engine

    def search(query: str, k: int) -> list[dict]:
        r = engine().search(query, folder="", k=k)
        return r.get("hits") or []

    floor = check.dense_floor(str(CONFIG.embedding_model or ""))
    if floor is None:
        return search, None, None
    try:
        from silica.model.providers import get_embedder
        from silica.kernel.check.dense import leg

        dense = leg(get_embedder(CONFIG))
    except Exception:
        return search, None, None  # no embedder configured: the dense leg is simply absent
    if dense is None:
        return search, None, None
    return search, dense, floor


def _epistemic_lanes(vault: str) -> tuple[list[tuple[str, str]], list[str]]:
    """(attested notes whose body moved on, gate-written notes edited outside
    Silica) for `/stale` (spec M4). Same walk rules as the OKF census: hidden
    folders skipped."""
    from pathlib import Path

    from silica.kernel.write.notetype import attestation_drift
    from silica.kernel.write.undo_journal import edited_since_write

    root = Path(vault)
    drifted: list[tuple[str, str]] = []
    for f in sorted(root.rglob("*.md")):
        rel = f.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        try:
            at = attestation_drift(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if at:
            drifted.append((rel.as_posix(), at))
    try:
        edited = edited_since_write(vault)
    except Exception as e:  # a corrupt or locked journal must not hide the code lane
        logger.warning("/stale: edited-since-write lane skipped: %s", e)
        edited = []
    return drifted, edited


def _print_epistemic_lanes(vault: str) -> int:
    """Prints the two epistemic lanes; returns how many notes they named."""
    drifted, edited = _epistemic_lanes(vault)
    for rel, at in drifted:
        CONSOLE.print(f"  · [bold]{rel}[/] — attested {at}, body changed since (re-run /verify or /verify --clear)")
    for rel in edited:
        CONSOLE.print(f"  · [bold]{rel}[/] — edited outside Silica since the gate wrote it")
    return len(drifted) + len(edited)


def _dc_stale(args: list[str], **_) -> bool:
    """/stale [--all] — code notes whose source moved on, attested notes whose
    body moved on, gate-written notes edited by hand since.
    /stale --stamp [--write] — derive documents:/code_ref for unbound notes (ADR-0038)."""
    from pathlib import Path
    vault = CONFIG.vault_path
    if not vault:
        CONSOLE.print("  No vault configured; /stale needs a .silica vault in a git repo.")
        return True
    if "--stamp" in args:
        folder = next((a for a in args if not a.startswith("--")), "")
        return _stale_stamp(Path(vault), folder=folder, write="--write" in args)
    # One all-clear for the whole command, and only when every lane is empty:
    # a per-lane "nothing here" read as a verdict above the next lane's list.
    shown = _stale_code_lane(Path(vault), show_all="--all" in args)
    shown += _print_epistemic_lanes(vault)
    if not shown:
        CONSOLE.print("  Nothing stale: every documents: note matches its code_ref, every "
                      "attestation matches its body, no gate-written note edited by hand.")
    return True


def _stale_code_lane(vault, *, show_all: bool) -> int:
    """Prints the code lane; returns how many notes it named."""
    from pathlib import Path

    from silica.kernel.code import codedocs
    # /stale is the manual refresh valve: drop the cache, recompute, rewrite.
    codedocs.invalidate_snapshot(Path(vault))
    stale = codedocs.snapshot(Path(vault))
    by_note: dict[str, list] = {}
    for sd in stale:
        by_note.setdefault(sd.note_path, []).append(sd)
    shown = 0
    for note_path, docs in sorted(by_note.items()):
        level, details = codedocs.note_verdict(docs)
        if level != codedocs.CHANGE_STRUCTURAL and not show_all:
            continue
        shown += 1
        CONSOLE.print(f"  · [bold]{note_path}[/] — {level}")
        for sd in docs:
            n = len(sd.intervening)
            CONSOLE.print(
                f"      documents [bold]{sd.code_path}[/] — {n} new commit(s) "
                f"since {sd.recorded_ref[:8]}"
            )
        for d in details[:6]:
            CONSOLE.print(f"      {d}")
    if not shown:
        hidden = len(by_note)
        if hidden and not show_all:
            CONSOLE.print(
                f"  No structural staleness. {hidden} note(s) have cosmetic-only "
                "changes — use [bold]/stale --all[/] to list them."
            )
        return 0
    CONSOLE.print("  Run [bold]/nucleate <path>[/] to regenerate, or edit and re-badge.")
    return shown


def _stale_stamp(vault, folder: str, write: bool) -> bool:
    """List (and with `write`, apply) derived bindings. The write is a yes
    given per run and per folder: detection may start by itself, a stamp may
    not (ADR-0038)."""
    from silica.kernel.code import derive_documents as dd
    props = dd.propose(vault, folder=folder)
    if not props:
        where = f" under {folder}" if folder else ""
        CONSOLE.print(f"  Nothing to stamp{where}: every note that names source already carries documents:.")
        return True
    for p in props:
        more = f" +{len(p.documents) - 4}" if len(p.documents) > 4 else ""
        CONSOLE.print(f"  · [bold]{p.note_path}[/] ← {', '.join(p.documents[:4])}{more}"
                      f"  @ {p.code_ref[:8]} ({'+'.join(p.lanes)})")
    if write:
        n = dd.apply(vault, props)
        CONSOLE.print(f"  Stamped {n} note(s). Run [bold]/stale[/] for the drift since each code_ref.")
    else:
        CONSOLE.print(f"  {len(props)} proposal(s), nothing written. "
                      "Add [bold]--write[/] to stamp them.")
    return True


def _dc_migrate(args: list[str], **_) -> bool:
    """/migrate [folder] [--write]: notes Silica wrote in an older shape,
    rewritten through today's writer (ADR-0039). A list by default; the write
    is a yes per run and per folder and lands as one revertible run."""
    from pathlib import Path

    from silica.kernel.write import migrate
    vault = CONFIG.vault_path
    if not vault:
        CONSOLE.print("  No vault configured; /migrate needs one.")
        return True
    folder = next((a for a in args if not a.startswith("--")), "")
    todo = written = 0
    # One shape after the other, the listing of the second taken after the
    # first has written: a note the first rewrites can then carry the second.
    # One journal run per shape, so /revert peels them back one at a time.
    for m in migrate.MIGRATIONS:
        plans = migrate.propose(Path(vault), folder=folder, migration=m)
        if not plans:
            continue
        CONSOLE.print(f"  [bold]{m.name}[/]")
        for p in plans:
            tag = f"  skipped: {p.skip}" if p.skip else ""
            CONSOLE.print(f"  · [bold]{p.path}[/]  {p.hits} block(s){tag}")
        todo += sum(1 for p in plans if not p.skip)
        if "--write" in args:
            n, run_id = migrate.apply(Path(vault), plans, migration=m)
            written += n
            if run_id:
                CONSOLE.print(f"  Migrated {n} note(s) (run {run_id[:8]}). [bold]/revert[/] undoes it.")
    if not todo:
        where = f" under {folder}" if folder else ""
        CONSOLE.print(f"  Nothing to migrate{where}: no note carries an older shape.")
    elif "--write" not in args:
        CONSOLE.print(f"  {todo} note(s) to rewrite, nothing written. "
                      "Add [bold]--write[/] to migrate them.")
    return True

def _dc_impact(args: list[str], **_) -> bool:
    """/impact [<git-range>] — which notes a code change touches."""
    from pathlib import Path
    from silica.kernel.code.codegraph import compute_impact
    vault = CONFIG.vault_path
    if not vault:
        CONSOLE.print("  No vault configured; /impact needs a vault inside a git repo.")
        return True
    range_spec = args[0] if (len(args) + 1) > 1 else None
    entries = compute_impact(Path(vault), range_spec)
    if entries is None:
        CONSOLE.print("  No git repo — impact analysis unavailable.")
        return True
    if not entries:
        scope = range_spec or "working tree vs HEAD"
        CONSOLE.print(f"  No supported source files changed ({scope}).")
        return True
    for e in entries:
        CONSOLE.print(f"  · [bold]{e.path}[/] — {e.change_level} (fan-in {e.fan_in})")
        for d in e.details[:4]:
            CONSOLE.print(f"      {d}")
        if e.notes:
            CONSOLE.print(f"      documents: {', '.join(e.notes)}")
        if e.neighbor_notes:
            CONSOLE.print(f"      1-hop neighbors documented by: {', '.join(e.neighbor_notes)}")
    return True


def _dc_plans(args: list[str], **_) -> bool:
    """/plans — the plan ledger: what is queued, running, done."""
    from pathlib import Path

    from rich.markup import escape

    from silica.kernel import plans as plans_mod
    if not CONFIG.vault_path:
        CONSOLE.print("  No vault configured; /plans needs a .silica vault.")
        return True
    vault = Path(CONFIG.vault_path)
    counts = plans_mod.status_counts(vault)
    if not counts:
        CONSOLE.print("  No plans found under plans/.")
        return True
    summary = ", ".join(f"[bold]{n}[/] {s}" for s, n in sorted(counts.items()))
    CONSOLE.print(f"  Plans: {summary}")
    for note_path, data in plans_mod.iter_plan_notes(vault):
        status = str(data.get("status") or "?").strip()
        # escape() keeps the literal [status] bracket from being parsed as
        # rich markup (otherwise [todo] is swallowed as an unknown tag).
        CONSOLE.print(f"    {escape(f'[{status}] {note_path.stem}')}")
    return True


def _dc_path(args: list[str], *, raw_input: str = "", **_) -> bool:
    """/path <noteA> <noteB> — the shortest link path between two notes."""
    from silica.kernel.graph.mindmap import note_resolver, reading_path
    try:
        toks = shlex.split(raw_input.strip())[1:]  # honours quoted titles with spaces
    except ValueError:
        CONSOLE.print('  Unbalanced quotes. Usage: /path "<note A>" "<note B>"')
        return True
    endpoints = _positional(toks)
    if len(endpoints) != 2:
        CONSOLE.print("  Usage: /path <noteA> <noteB>")
        return True
    resolve = note_resolver()
    src, dst = resolve(endpoints[0]), resolve(endpoints[1])
    for given, got in zip(endpoints, (src, dst)):
        if got is None:
            CONSOLE.print(f"  Note not found: '{given}'")
    if src is None or dst is None:
        return True
    if src == dst:
        CONSOLE.print("  Both resolve to the same note — nothing to walk.")
        return True
    # Weighted: a reading path wants the most coherent chain, not the fewest
    # hops — A/B on a live vault: weakest-link 0.87→0.97 for +0.14 hops.
    from silica.kernel.recall.requires import written_requires

    try:
        requires = written_requires(CONFIG.vault_path)
    except Exception:
        requires = {}  # an unreadable walk: the path runs on links and co-occurrence, as before
    path = reading_path(src, dst, weighted=True, requires=requires)
    if path is None:
        CONSOLE.print(
            f"  No path between [bold]{src}[/] and [bold]{dst}[/] — "
            "not connected (try /map on each to see its neighborhood)."
        )
        return True
    CONSOLE.print(f"  Reading path — {len(path) - 1} step(s):")
    for i, (node, leg) in enumerate(path):
        if leg != "start":
            CONSOLE.print(f"        [dim]↓ {leg}[/]")
        CONSOLE.print(f"    {i + 1}. [bold]{node}[/]")
    return True


def _dc_contested(args: list[str], **_) -> bool:
    """/contested — notes flagged with an unresolved contradiction."""
    from silica.driver import DRIVER
    from silica.kernel.base.layout import vault_epoch
    from silica.kernel.write.contested import CONTESTED_KEY, CONTRADICTIONS_KEY

    # The scan frontmatter-parses every note, so it runs once per
    # file-state epoch; between vault changes a repeat costs a stat walk.
    epoch = vault_epoch()
    hit = _CONTESTED_SCAN_MEMO.get("scan") if epoch else None
    found: list[tuple[str, list[str]]] | None = (
        hit[1] if hit is not None and hit[0] == epoch else None)
    if found is None:
        found = []
        for ref in DRIVER.list_files(""):
            try:
                props = DRIVER.props_of(ref.path)
            except Exception:
                continue  # attachments / unreadable frontmatter — not contested
            if props and props.get(CONTESTED_KEY):
                contras = [str(c) for c in (props.get(CONTRADICTIONS_KEY) or [])]
                found.append((ref.path, contras))
        if epoch:
            _CONTESTED_SCAN_MEMO["scan"] = (epoch, found)
    if not found:
        CONSOLE.print("  No contested notes — no unresolved contradictions.")
        return True
    CONSOLE.print(f"  {len(found)} contested note(s):")
    for note_path, contras in sorted(found):
        CONSOLE.print(f"  · [bold]{note_path}[/]")
        for c in contras:
            CONSOLE.print(f"      conflicts with: {c}")
    CONSOLE.print(
        "  Ask the agent to resolve one with silica_flag_note(clear=True, "
        "ref=…), passing a `conflicts with` line above verbatim; the losing "
        "claim is filed under `## Superseded` instead of being deleted."
    )
    return True


def _dc_agenda(args: list[str], **_) -> bool:
    """/agenda [days] — dated commitments coming up."""
    from rich.markup import escape

    from silica.tools.events import silica_agenda

    arg = " ".join(args).strip().casefold()
    # "week" is the default window spelled out; a date moves the start.
    start = "today" if arg in ("", "today", "week") else arg
    res = silica_agenda(start=start, days=7)
    if res.get("error"):
        CONSOLE.print(f"  [yellow]{escape(res['error'])}[/]")
        return True
    CONSOLE.print(escape(res["text"]))
    return True


def _dc_episodes(args: list[str], *, raw_input: str = "", **_) -> bool:
    """/episodes [key] — what episodic memory has accumulated."""
    from rich.markup import escape

    from silica.kernel.recall.episodic import EpisodicStore, FactHit, render

    store = EpisodicStore()
    heads = sorted(store.live_facts(), key=lambda f: f.key)
    if not heads:
        CONSOLE.print("  No episodic memory yet — nothing has been captured.")
        return True
    body = "\n\n".join(
        f"## {h.key}\n" + render([FactHit(fact=h, score=1.0)], store=store)
        for h in heads
    )
    CONSOLE.print(escape(body))
    # Re-split the raw line with shlex so `--save="a b/x.md"` honours the
    # quoted path; a malformed quote falls back to the whitespace split.
    # (shlex is imported at module scope — a local `import shlex` here
    # rebinds the name for the WHOLE function, and /path's use of it above
    # then dies with UnboundLocalError before this branch ever runs.)
    try:
        save_args = shlex.split(raw_input.strip())[1:]
    except ValueError:
        save_args = args
    save = _str_flag(save_args, "--save")
    if save:
        from pathlib import Path

        out = Path(save).expanduser().resolve()
        # Empty until a vault is adopted, and Path("") resolves to the cwd:
        # no vault means nothing to fall inside, not "everything under here".
        raw_vault = (CONFIG.vault_path or "").strip()
        vault = Path(raw_vault).expanduser().resolve() if raw_vault else None
        if vault is not None and out.is_relative_to(vault):
            # The one door in stays the gate: an episodic render dropped
            # into the vault would be an unreviewed note that indexes,
            # links and gets recalled — the echo channel, by hand.
            CONSOLE.print(
                "  [yellow]Not inside the vault.[/] Session memory becomes a "
                "note through [bold]/promote <key>[/]; save this render "
                "somewhere else."
            )
            return True
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(f"# Episodes\n\n{body}\n", encoding="utf-8")
        except OSError as e:
            # The REPL calls this handler outside any try: a directory, a
            # read-only mount or a bad name would end the session.
            CONSOLE.print(f"  [yellow]save failed: {escape(str(e))}[/]")
            return True
        CONSOLE.print(f"  Saved → [bold]{escape(str(out))}[/]")
    return True


def _dc_changes(args: list[str], **_) -> bool:
    """/changes — every note this session wrote to, with its line tally."""
    from rich.markup import escape

    from silica.kernel.write import session_changes

    rows = session_changes.rows()
    if not rows:
        CONSOLE.print("  Nothing changed this session.")
        return True
    # Single letters, not words: the point of the column is that the eye skips it
    # unless something is unusual, and `git status`'s letters are already learned.
    letter = {"created": "A", "modified": "M", "deleted": "D", "moved": "R"}
    # Padding is computed on the plain text and applied by hand: rich markup adds
    # characters that f-string alignment would count, so `:>` would stagger the
    # columns by the length of whichever colour tag happens to be on the row.
    width = max(len(r["path"]) for r in rows)
    add_w = max(len(f"+{r['added']}") for r in rows)
    del_w = max(len(f"-{r['removed']}") for r in rows)
    total_add = total_del = 0
    for r in rows:
        total_add += r["added"]
        total_del += r["removed"]
        plus = f"+{r['added']}" if r["added"] else ""
        minus = f"-{r['removed']}" if r["removed"] else ""
        cells = (f"{' ' * (add_w - len(plus))}[green]{plus}[/] "
                 f"[red]{minus}[/]{' ' * (del_w - len(minus))}")
        origin = f"  [dim]← {escape(r['from'])}[/]" if r["from"] else ""
        CONSOLE.print(
            (f"  [dim]{letter.get(r['kind'], '?')}[/] [bold]{escape(r['path'])}[/]"
             f"{' ' * (width - len(r['path']))}  {cells}{origin}").rstrip()
        )
    CONSOLE.print(
        f"  [dim]{len(rows)} note(s), +{total_add} -{total_del} — "
        f"/undo <note-path> takes one back, /revert the whole run.[/]"
    )
    return True


def _dc_undo(args: list[str], *, raw_input: str = "", **_) -> bool:
    """/undo [note-path]: revert the last write to one note, bring back a
    note the chat or /delete deleted, or move back a note the chat moved."""
    from silica.driver import DRIVER
    from silica.kernel.write.checkpoints import get_checkpoint_store
    from silica.kernel.link.sweep import revert_and_sweep
    from silica.kernel.write.undo_journal import USER_DELETE_SOURCE, get_undo_journal

    store = get_checkpoint_store()
    journal = get_undo_journal()
    vault = (CONFIG.vault_path or "").strip() or None

    def _on_disk(p: str) -> bool:
        root = Path(vault) if vault else None
        return root is None or (root / p).exists() or (root / f"{p}.md").exists()

    # Everything after the command IS the path: `args` is a plain whitespace
    # split, so `/undo silica/Verdetto reranker.md` used to look up
    # "silica/Verdetto" and report nothing to undo. Note names with spaces are
    # the common case in a vault, and the web GUI's per-note revert sends this.
    def _written_since(p: str, t: float) -> bool:
        return any((store.latest_at(q) or 0.0) > t for q in (p, f"{p}.md"))

    rest = raw_input.strip().split(maxsplit=1)
    deleted = moved = None
    note_path: str | None
    if len(rest) > 1:
        note_path = _unquoted(rest[1])
        if store.depth(note_path) == 0:
            # The write card sends the tool's own argument, often a bare name,
            # while the store keys a patch on the path it resolved to: unresolved,
            # the card's revert said "nothing to undo" over a patch that had
            # landed (2026-10-26).
            try:
                resolved = DRIVER.read_note(note_path).ref.path
            except Exception:
                # No note answers to the name: a deleted note is looked up by
                # the name as given, just below.
                resolved = ""
            if resolved and store.depth(resolved):
                note_path = resolved
        if not _on_disk(note_path):
            deleted = journal.last_delete(vault, note_path)
        # A patch the note took after it landed is undone before the move.
        move = None if deleted else journal.last_chat_move(vault, note_path)
        if move and not _written_since(move[2], move[3]):
            moved = move
    else:
        # The newest action wins, a write, a delete or a move: a delete and a
        # move live in the undo journal, not in the checkpoint store.
        last_write = store.most_recent()
        newest = last_write[1] if last_write else None
        last_delete = journal.last_delete(vault)
        if last_delete and not _on_disk(last_delete[1]) and (
                newest is None or last_delete[2] >= newest):
            deleted, newest = last_delete, last_delete[2]
        last_move = journal.last_chat_move(vault)
        if last_move and (newest is None or last_move[3] >= newest):
            deleted, moved = None, last_move
        note_path = deleted[1] if deleted else (last_write[0] if last_write else None)
    if moved:
        res = revert_and_sweep(moved[0])
        if res["reverted"]:
            CONSOLE.print(f"  Moved back: [bold]{moved[2]}[/] → {moved[1]}")
        else:
            why = (res["skipped"] or res["errors"] or [{"reason": "unknown"}])[0]
            CONSOLE.print(f"  [yellow]Not moved back:[/] {moved[2]} "
                          f"({why.get('reason') or why.get('error')})")
        return True
    if deleted:
        user_deleted = (journal.run_info(deleted[0]) or {}).get("source") == USER_DELETE_SOURCE
        res = revert_and_sweep(deleted[0])
        if res["reverted"]:
            # user:/delete is /delete and the tree's row menu alike (ADR-0123):
            # naming the command misnamed a delete the person made with a click
            how = "you deleted it" if user_deleted else "deleted from the chat"
            CONSOLE.print(f"  Restored: [bold]{deleted[1]}[/] ({how})")
        else:
            why = (res["skipped"] or res["errors"] or [{"reason": "unknown"}])[0]
            CONSOLE.print(f"  [yellow]Not restored:[/] {deleted[1]} "
                          f"({why.get('reason') or why.get('error')})")
        return True
    if not note_path:
        CONSOLE.print("  Nothing to undo — no patches recorded in this session.")
        return True

    try:
        on_disk = DRIVER.read_note(note_path).content
    except Exception:
        on_disk = None  # nothing on disk to keep: the undo writes the note back
    since = store.changed_since(note_path, on_disk)
    # Read before the undo: written over blind, an edit made in Obsidian after
    # Silica's write was lost for good (ADR-0144, reproduced 2026-11-01).
    content = store.undo(note_path, current=on_disk)
    if content is None:
        CONSOLE.print(f"  [yellow]Nothing to undo for[/] {note_path} (already at original).")
        return True
    kept = {"hand": "; your edits made after it are taken back too and kept in the note's history",
            "run": "; a later run's changes to it are taken back too and kept in the note's history",
            }.get(since or "", "")

    if store.absent_floor(note_path):
        # The chat created this note: before it there was nothing, so undo
        # removes it rather than leave a 0-byte note that keeps taking links.
        # Removed with a restore point, so a second /undo brings it back: on
        # hosted t06 (2026-10-22) a write card's revert removed a 21k-char note
        # the turn had written and nothing in the app could restore it.
        from silica.kernel.write.gate import delete_with_restore_point

        try:
            res = delete_with_restore_point(
                note_path, DRIVER.read_note(note_path).content, USER_DELETE_SOURCE)
        except Exception as exc:
            res = {"error": str(exc)}
        if "error" in res:
            CONSOLE.print(f"  [red]Undo failed:[/] {res['error']}")
            return True
        store.clear(note_path)
        CONSOLE.print(f"  Undone: [bold]{note_path}[/] removed (the chat created it); "
                      f"/undo {note_path} brings it back")
        return True
    from silica.kernel.write.preserve import Loss

    try:
        DRIVER.overwrite(note_path, content, loss=Loss.RESTORE)
        try:
            store.settle(note_path, DRIVER.read_note(note_path).content)  # what landed, stamp included
        except Exception:
            pass  # the undo landed; an unsettled top only labels the next write as an outside edit
        depth = store.depth(note_path)
        remaining = max(0, depth - 1)
        CONSOLE.print(f"  Undone: [bold]{note_path}[/]{kept}  [dim]({remaining} undo step(s) remaining)[/]")
    except Exception as exc:
        CONSOLE.print(f"  [red]Undo failed:[/] {exc}")
    return True


def _dc_delete(args: list[str], *, raw_input: str = "", **_) -> bool:
    """/delete <path>: delete a note as the user's own act (ADR-0092). The
    model's silica_delete refuses a note other notes link to; this is where
    the user deletes it, with no model in between, the way /remember writes
    past the memory gate. Journaled under user:/delete, so /undo brings it
    back; it prints each link the delete left pointing to no note and each
    note it left with no incoming link."""
    from rich.markup import escape

    from silica.driver import DRIVER
    from silica.kernel.write.gate import delete_with_restore_point
    from silica.kernel.write.undo_journal import USER_DELETE_SOURCE

    # Everything after the command is the path, as for /undo: note names
    # with spaces are the common case.
    rest = raw_input.strip().split(maxsplit=1)
    ref = _unquoted(rest[1]) if len(rest) > 1 else ""
    if not ref:
        CONSOLE.print("  Usage: /delete <path>  [dim](the note to delete; /undo brings it back)[/]")
        return True
    try:
        nc = DRIVER.read_note(ref)
        path = nc.ref.path or ref
        res = delete_with_restore_point(path, nc.content, USER_DELETE_SOURCE)
    except Exception as exc:
        res = {"error": str(exc)}
    if "error" in res:
        err = res["error"].removeprefix("not deleted: ")
        CONSOLE.print(f"  [yellow]Not deleted:[/] {escape(ref)} ({escape(err)})", soft_wrap=True)
        return True
    # soft_wrap: a vault path is long, and a link cut across two lines in
    # the GUI's code block reads as two entries.
    CONSOLE.print(f"  Deleted: [bold]{escape(path)}[/]", soft_wrap=True)
    if dangling := res["dangling"]:
        CONSOLE.print(f"  {len(dangling)} link{'s' if len(dangling) != 1 else ''} now point"
                      f"{'' if len(dangling) != 1 else 's'} to no note:")
        for d in dangling:
            CONSOLE.print(f"    {escape(d['note'])} → {escape('[[' + d['link'] + ']]')}",
                          soft_wrap=True)
    if orphaned := res["orphaned"]:
        CONSOLE.print(f"  {len(orphaned)} note{'s' if len(orphaned) != 1 else ''} left with "
                      "no incoming link:")
        for o in orphaned:
            CONSOLE.print(f"    {escape(o)}", soft_wrap=True)
    CONSOLE.print(f"  [dim]In the trash: /undo {escape(path)} brings it back.[/]", soft_wrap=True)
    return True


def _dc_trash(args: list[str], *, raw_input: str = "", **_) -> bool:
    """/trash [restore <path> | erase <path> | empty]: the notes deleted from
    this vault, by the user or the chat, that nothing has brought back
    (ADR-0128). restore brings one note back, not the run it was deleted in;
    erase and empty delete for good what the trash holds."""
    from datetime import datetime as _dt

    from rich.markup import escape

    from silica.kernel.base.notekey import note_key
    from silica.kernel.write.gate import restore_from_trash
    from silica.kernel.write.undo_journal import USER_DELETE_SOURCE, get_undo_journal

    journal = get_undo_journal()
    items = journal.trashed((CONFIG.vault_path or "").strip() or None)
    rest = raw_input.strip().split(maxsplit=2)
    verb = rest[1].lower() if len(rest) > 1 else ""
    if verb in ("", "empty") and not items:
        CONSOLE.print("  The trash is empty.")
        return True
    if not verb:
        CONSOLE.print(f"  Trash: {len(items)} note{'s' if len(items) != 1 else ''}")
        for t in items:
            when = _dt.fromtimestamp(t["deleted_at"]).strftime("%Y-%m-%d %H:%M")
            who = "you" if t["source"] == USER_DELETE_SOURCE else "the chat"
            CONSOLE.print(f"    {escape(t['path'])}  [dim]{when}, {who}[/]", soft_wrap=True)
        CONSOLE.print("  [dim]/trash restore <path> brings one back, /trash erase <path> "
                      "or /trash empty deletes for good.[/]")
        return True
    if verb == "empty":
        for t in items:
            journal.leave_trash(t["run"], t["path"])
        CONSOLE.print(f"  Deleted for good: {len(items)} note{'s' if len(items) != 1 else ''}.")
        return True
    if verb not in ("restore", "erase") or len(rest) < 3:
        CONSOLE.print("  Usage: /trash [restore <path> | erase <path> | empty]")
        return True
    # Everything after the verb is the path, as for /undo. A path deleted
    # twice is two items; the newest delete is the one meant.
    want = note_key(_unquoted(rest[2]))
    item = next((t for t in items if note_key(t["path"]) == want), None)
    if item is None:
        CONSOLE.print(f"  [yellow]Not in the trash:[/] {escape(_unquoted(rest[2]))}", soft_wrap=True)
        return True
    if verb == "erase":
        journal.leave_trash(item["run"], item["path"])
        CONSOLE.print(f"  Deleted for good: [bold]{escape(item['path'])}[/]", soft_wrap=True)
        return True
    why = restore_from_trash(item, store=journal)
    if why:
        CONSOLE.print(f"  [yellow]Not restored:[/] {escape(item['path'])} ({escape(why)})",
                      soft_wrap=True)
    else:
        CONSOLE.print(f"  Restored: [bold]{escape(item['path'])}[/]", soft_wrap=True)
    return True


def _revert_by_source(source: str, vault: str | None) -> bool:
    """Every journalled run the ledger attributes to `source`, newest first,
    scoped to that source's notes; what the journal does not hold is named,
    never guessed at (legacy runs predate the join and stay /revert <run-id>)."""
    from rich.markup import escape

    from silica.kernel.link.sweep import sweep_survivors
    from silica.kernel.write.gate import revert_source

    res = revert_source(source, vault=vault, after_run=sweep_survivors)
    if res.get("ambiguous"):
        CONSOLE.print(f"  {len(res['ambiguous'])} sources are named {escape(source)}; "
                      "nothing reverted. Name one:", soft_wrap=True)
        for k in res["ambiguous"]:
            CONSOLE.print(f"    /revert --source {escape(k)}", soft_wrap=True)
        return True
    runs = res["runs"]
    reverted = sum(len(r["reverted"]) for r in runs)
    skipped = sum(len(r["skipped"]) for r in runs)
    errors = sum(len(r["errors"]) for r in runs)
    left = sum(u["notes"] for u in res["unrevertable"])
    line = (f"  Revert by source {source}: {len(runs)} run(s), {reverted} writes "
            f"reverted, {skipped} skipped, {errors} errors.")
    if res["unrevertable"]:
        line += (f" {len(res['unrevertable'])} run(s) not in the undo journal: "
                 f"{left} note(s) left in place (/revert <run-id> still applies "
                 f"to journalled runs).")
    if not runs and not res["unrevertable"]:
        line = f"  Nothing to revert — the ledger has no record of {source}."
    CONSOLE.print(line)
    try:
        from silica.kernel.write.run_log import append_log_line, format_revert_event
        for r in runs:
            append_log_line(
                format_revert_event(source, len(r["reverted"]), len(r["skipped"])),
                r["run_id"],
            )
    except Exception:
        pass  # the journal is a courtesy, never a failure path
    return True


def _dc_revert(args: list[str], *, raw_input: str = "", **_) -> bool:
    """/revert [run-id | --source <file>] — undo a journalled run, or every run
    that derived notes from one source."""
    from rich.markup import escape

    from silica.kernel.write.undo_journal import get_undo_journal
    vault = CONFIG.vault_path.strip() or None
    tokens = list(args) or raw_input.strip().split()[1:]
    # The source is the rest of the line: lecture files carry spaces
    # ("CorsoA/Lezione 1.md"), and one token cut every such name at its first.
    source = ""
    for i, t in enumerate(tokens):
        if t == "--source" or t.startswith("--source="):
            source = " ".join([t.split("=", 1)[1], *tokens[i + 1:]]) if "=" in t else " ".join(tokens[i + 1:])
            break
    source = source.strip().strip("\"'")
    if source:
        return _revert_by_source(source, vault)
    parts_split = raw_input.strip().split(maxsplit=1)
    given = _unquoted(parts_split[1]) if len(parts_split) > 1 else ""
    if not given:
        run_id = _latest_revertable(vault)
        if not run_id:
            CONSOLE.print("  Nothing to revert — no runs recorded for this vault.")
            return True
        return _revert_one(run_id)
    found = get_undo_journal().match_runs(given, vault)
    ledgers = {ledger for _run, ledger in found}
    if any(run == given for run, _l in found):
        runs = [given]
    elif found and len(ledgers) == 1 and (next(iter(ledgers)) or "").startswith(given.lower()):
        # The id /status prints is the ledger's, and one ledger run can hold
        # several journal runs: newest first, the only sound replay order.
        runs = [run for run, _l in found]
    elif len(found) == 1:
        runs = [found[0][0]]
    elif found:
        CONSOLE.print(f"  '{escape(given)}' names {len(found)} runs; give more of the id:")
        for run, ledger in found[:10]:
            CONSOLE.print(f"    {run}  [dim](run {(ledger or '')[:8]})[/]")
        return True
    else:
        # Not a journal run: a Zotero write's run, which has no inverse in the
        # journal and is taken back by _revert_zotero alone, keeps working as
        # it did when every id passed straight through.
        runs = [given]
    for run_id in runs:
        _revert_one(run_id)
    return True


def _revert_one(run_id: str) -> bool:
    """One run taken back, journal and Zotero, and the line that says what it was."""
    from silica.kernel.link.sweep import revert_and_sweep
    from silica.kernel.write.undo_journal import get_undo_journal

    # Name WHAT is being reverted: the journal's run ids live in a different
    # id-space than the progress ledger's (log.md), so a bare id told the
    # user nothing about which run they were about to undo.
    info = None
    try:
        info = get_undo_journal().run_info(run_id)
    except Exception:
        pass
    source = (info or {}).get("source") or ""
    when = ""
    if info and info.get("started_at"):
        from datetime import datetime as _dt
        when = _dt.fromtimestamp(info["started_at"]).strftime("%Y-%m-%d %H:%M")
    label = f" ({source}, started {when})" if source and when else ""
    res = revert_and_sweep(run_id)
    _revert_zotero(run_id)
    stale = len(res.get("stale", []))
    line = (
        f"  Revert {run_id[:8]}…{label}: {len(res['reverted'])} writes reverted, "
        f"{len(res['skipped'])} skipped (modified), "
        f"{stale} stale (vault changed), {len(res['errors'])} errors."
    )
    CONSOLE.print(line)
    # log.md narrated the run's writes; it must narrate the take-back too.
    try:
        from silica.kernel.write.run_log import append_log_line, format_revert_event
        append_log_line(
            format_revert_event(source, len(res["reverted"]), len(res["skipped"])),
            run_id,
        )
    except Exception:
        pass  # the journal is a courtesy, never a failure path
    return True


def _latest_revertable(vault: str | None) -> str | None:
    """The newest run with anything to undo. A run that only wrote to Zotero
    (/zotero file --apply) has no inverse in the undo journal, which holds
    vault files only, so the journal alone would skip it (ADR-0055 decision 8)."""
    from silica.kernel.write.undo_journal import get_undo_journal
    from silica.sources import zotero_write

    journal = get_undo_journal()
    newest = journal.last_active_run(vault=vault)
    zotero_run = zotero_write.last_run()
    if not zotero_run:
        return newest
    if not newest:
        return zotero_run
    started = (journal.run_info(newest) or {}).get("started_at") or 0
    return zotero_run if zotero_write.run_started(zotero_run) > started else newest


def _revert_zotero(run_id: str) -> None:
    from silica.sources import zotero, zotero_write

    if not zotero_write.has_run(run_id):
        return
    try:
        res = zotero_write.revert(zotero.client(), run_id)
    except zotero.ZoteroError as exc:
        CONSOLE.print(f"  [yellow]Zotero writes of this run not reverted: {exc}[/]")
        return
    CONSOLE.print(f"  Zotero: {res['undone']} write(s) undone.")
    for key, why in res["left"]:
        CONSOLE.print(f"  [yellow]left alone[/]  {key}: {why}")


def _dc_zotero(args: list[str], *, raw_input: str = "", **_) -> bool:
    """/zotero [file|group|unlink|grant] [--apply|--yes]: the linked library (ADR-0055)."""
    from silica.sources import zotero_cli

    zotero_cli.run(list(args) or raw_input.strip().split()[1:], CONSOLE)
    return True


def _dc_mail(args: list[str], *, raw_input: str = "", **_) -> bool:
    """/mail [import <file> ...]: mail threads from a file the user exported (ADR-0090)."""
    from silica.sources import mail_cli

    # The raw line, not `args`: a Takeout file is "All mail Including Spam
    # and Trash.mbox", and whitespace splitting would cut it in five.
    mail_cli.run(raw_input or " ".join(["/mail", *args]), CONSOLE)
    return True


def _dc_connect(args: list[str], *, raw_input: str = "", ask=None, **_) -> bool:
    """/connect [list|add <json>|secure|login|logout|remove <key>|reload]: MCP servers (ADR-0091)."""
    from silica.connectors import connect_cli

    # The raw line: `add` takes a JSON object whose spaces whitespace
    # splitting would scatter.
    asks = connect_cli.terminal_asks() if ask else {}
    connect_cli.run(raw_input or " ".join(["/connect", *args]), CONSOLE, **asks)
    return True


def _dc_review(args: list[str], **_) -> bool:
    """/review [--flush=<hash>] — the deferred store: write bundles parked
    by a gate, listed or flushed. Not the study queue: that is /learn."""
    from silica.kernel.write.deferred import get_deferred_store
    store = get_deferred_store()
    flush_hash = _str_flag(args, "--flush")
    if flush_hash:
        removed = store.remove(flush_hash)
        if removed:
            CONSOLE.print(f"  Flushed bundle [bold]{flush_hash[:12]}[/] from review queue.")
        else:
            CONSOLE.print(f"  [yellow]No bundle with hash {flush_hash[:12]} found.[/]")
        return True
    items = store.list_all()
    if not items:
        CONSOLE.print("  Review queue is empty.")
    else:
        CONSOLE.print(f"  [bold]Review queue — {len(items)} bundle(s):[/]")
        for item in items:
            import datetime as _dt
            ts = _dt.datetime.fromtimestamp(item["timestamp"]).strftime("%Y-%m-%d %H:%M")
            CONSOLE.print(
                f"  · [bold]{item['content_hash'][:12]}[/]  {item['source_path']}  "
                f"({item['rejected_count']} op(s))  {ts}"
            )
        CONSOLE.print("  Use [bold]/review --flush=<hash>[/] to discard a bundle.")
    return True


def _dc_anneal(args: list[str], **_) -> bool:
    """/anneal [--steer] [--limit=N] — sweep the deferred queue.

    The counterpart to /review, which only looks. Kept a direct command and not
    an agent turn: the sweep is mechanical (re-validate, write what now passes),
    so routing it through the model would only add a round-trip. --steer is the
    one leg that calls a model: a bounded repair loop per bundle that still fails.
    """
    from silica.tools import TOOLS

    steer = any(p == "--steer" for p in args)
    limit = _int_flag(args, "--limit=", 0)
    scope = f"{limit} bundle(s)" if limit else "every bundle"
    CONSOLE.print(f"  Annealing {scope}{' with escalation' if steer else ''}…")
    res = json.loads(TOOLS["silica_anneal"].run(steer=steer, limit=limit))
    if "error" in res:
        CONSOLE.print(f"  [yellow]{res['error']}[/]")
        return True

    if not res.get("bundles"):
        CONSOLE.print("  Review queue is empty — nothing to anneal.")
        return True

    for row in res.get("results", []):
        mark = "cleared" if row.get("cleared") else f"{row['still_deferred']} left"
        line = f"  · [bold]{row['content_hash']}[/]  {row['written']} written, {mark}"
        if row.get("error"):
            line += f"  [yellow]{row['error']}[/]"
        CONSOLE.print(line)
    flagged = f", {res['flagged']} landed flagged for review" if res.get("flagged") else ""
    CONSOLE.print(
        f"  [bold]{res['bundles']}[/] bundle(s): {res['written']} op(s) written, "
        f"{res['still_deferred']} still deferred{flagged}."
    )
    # A sweep that wrote nothing and cleared nothing is the case worth naming:
    # silence there reads as success when the queue is untouched.
    if not res["written"] and res["still_deferred"] and not steer:
        CONSOLE.print("  [dim]Nothing became writable. Try [bold]/anneal --steer[/].[/]")
    return True


def _dc_curate(args: list[str], **_) -> bool:
    """/curate [folder] [--apply] — the curation pass over a folder."""
    from silica.tools import TOOLS

    apply = any(p == "--apply" for p in args)
    positional = _positional(args)
    folder = " ".join(positional)
    scope = folder or "(vault)"
    if apply:
        CONSOLE.print(f"  Curate on [bold]{scope}[/] — applying via the worker seam…")
    else:
        CONSOLE.print(f"  Curate on [bold]{scope}[/] — dry-run (nothing is written)…")
    res = json.loads(TOOLS["silica_curate"].run(apply=apply, folder=folder))
    if "error" in res:
        CONSOLE.print(f"  [yellow]{res['error']}[/]")
        return True

    total = res.get("total", 0)
    counts = res.get("counts", {})
    if total == 0:
        CONSOLE.print("  Nothing to do — the vault is coherent.")
        return True

    breakdown = ", ".join(f"{v} {k}" for k, v in counts.items())
    if apply:
        # Real outcomes (execution["outcome_counts"], derived from the
        # dispatch batch's per-item status + the mechanical autolink's
        # actual links-added count) — NOT the planned counts above, which
        # would report "Applied N" even when e.g. every dedup came back
        # a distinct verdict and nothing was actually merged.
        outcome = res.get("execution", {}).get("outcome_counts", {})
        dispatched = sum(outcome.values())
        outcome_breakdown = ", ".join(f"{v} {k}" for k, v in outcome.items()) or "no changes"
        CONSOLE.print(f"  Applied — dispatched [bold]{dispatched}[/] → outcomes: {outcome_breakdown}")
    else:
        CONSOLE.print(f"  Plan — [bold]{total}[/] item(s): {breakdown}")
        for it in res.get("items", []):
            pair = f" ↔ {it['partner']}" if it.get("partner") else ""
            CONSOLE.print(f"  · [bold]{it['kind']}[/]  {it['target']}{pair}")
        CONSOLE.print('  Run [bold]/curate --apply[/] to execute, or ask e.g. "apply only dedup".')
    return True


def _dc_aliases(args: list[str], **_) -> bool:
    """/aliases [note] — alias coverage, and what is missing."""
    from silica.tools import TOOLS

    apply = any(p == "--apply" for p in args)
    positional = _positional(args)
    folder = " ".join(positional)
    scope = folder or "(vault)"
    mode = "applying" if apply else "dry-run (nothing is written)"
    CONSOLE.print(f"  Alias consolidation on [bold]{scope}[/] — {mode}…")
    res = json.loads(TOOLS["silica_aliases"].run(apply=apply, folder=folder))
    if "error" in res:
        CONSOLE.print(f"  [yellow]{res['error']}[/]")
        return True
    groups = res.get("groups", {})
    if not groups:
        CONSOLE.print("  No alias groups survived the gate.")
        return True
    for canonical, variants in sorted(groups.items()):
        CONSOLE.print(f"  · [bold]{canonical}[/] ← {', '.join(variants)}")
    dropped = res.get("dropped", 0)
    if dropped:
        CONSOLE.print(f"  ({dropped} proposed variant(s) dropped by the ambiguity gate)")
    if apply:
        written = res.get("written", {})
        n = sum(written.values())
        CONSOLE.print(f"  Applied — [bold]{n}[/] alias(es) written into {len(written)} note(s).")
        for it in res.get("skipped", []):
            CONSOLE.print(f"  [yellow]skipped {it['note']}: {it['reason']}[/]")
    else:
        CONSOLE.print("  Run [bold]/aliases --apply[/] to write them into frontmatter.")
    return True


def _dc_keep(args: list[str], **_) -> bool:
    """/keep — save the last web answer as an inbox note."""
    from rich.markup import escape

    from silica.sources.web_research import keep_last

    try:
        note_rel = keep_last()
        CONSOLE.print(
            f"  Kept → [bold]{escape(note_rel)}[/]"
            "  (review, then /nucleate to bring it in)"
        )
    except Exception as e:  # empty slot, name collision, write refused
        CONSOLE.print(f"  [yellow]keep failed: {escape(str(e))}[/]")
    return True


def remember_text(raw_input: str) -> str:
    """What follows `/remember`, exactly as typed: the entry is the user's own
    words, so neither case nor inner spacing is touched."""
    parts = raw_input.strip().split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _dc_remember(args: list[str], *, raw_input: str = "", **_) -> bool:
    """/remember <text>: the user's words into this vault's memory with no
    model in between, so the taint gate does not apply (ADR-0076 §6): it is
    the way to save from a conversation that gate closed. A bare /remember
    is not handled here; it falls through to _SHORTCUTS and asks the model."""
    from rich.markup import escape

    from silica.kernel import user_memory

    text = remember_text(raw_input)
    if not text:
        return False
    from silica.obs import narration

    out = user_memory.save("vault", text, session=narration.NARRATOR.sid or "")
    if not out["ok"]:
        CONSOLE.print(f"  [yellow]{escape(out['error'])}[/]")
        for entry in out.get("entries") or ():
            CONSOLE.print(f"  [dim]{escape(entry)}[/]")
        return True
    CONSOLE.print(f"  {escape(user_memory.describe(out))}  "
                  f"[dim]({out['chars']}/{out['budget']} characters · /memory shows it all)[/]")
    return True


def _dc_memory(args: list[str], **_) -> bool:
    """/memory: both memory files with their paths and budgets: what
    Settings > Memory shows in the GUI, for a terminal that has no such page."""
    from rich.markup import escape

    from silica.kernel import user_memory

    view = user_memory.view()
    if view["paused"]:
        CONSOLE.print("  [yellow]memory is paused (SILICA_MEMORY=False): kept, not shown, not written[/]")
    elif view["incognito"]:
        CONSOLE.print("  [yellow]incognito: memory is kept but not shown or written this session[/]")
    home = str(Path.home())
    for scope, title in (("user", "you, in every vault"), ("vault", "this vault")):
        s = view["scopes"][scope]
        if not s["available"]:
            CONSOLE.print(f"  [bold]{title}[/]  [dim]no vault open[/]")
            continue
        where = s["path"].replace(home, "~", 1)
        CONSOLE.print(f"  [bold]{title}[/]  [dim]{escape(where)} · "
                      f"{s['chars']}/{s['budget']} characters[/]")
        body = s["text"].rstrip("\n")
        CONSOLE.print("\n".join(f"    {escape(line)}" for line in body.splitlines())
                      if body else "    [dim](empty)[/]")
    return True


def _dc_incognito(args: list[str], *, messages: list[dict] | None = None, **_) -> bool:
    """/incognito: this session off the record: not captured, and user
    memory neither shown to the model nor written (ADR-0076 §8). One flag per
    process, which is the TUI's session; the GUI clears it on a new chat."""
    from silica.kernel import user_memory

    on = not user_memory.incognito()
    user_memory.set_incognito(on)
    if on:
        from silica.obs import narration as _narr
        b = _narr.INCOGNITO_BEAT
        # No-op before the first turn: ensure_session marks the session at birth.
        _narr.NARRATOR.narrate(b["kind"], b["status"], b["summary"], {}, parent=None)
    had_slot = any(m.get("origin") == user_memory.MESSAGE_ORIGIN for m in messages or ())
    if messages is not None:
        user_memory.sync_live_snapshot(messages)
    if on:
        CONSOLE.print("  [dim]incognito: this session is not captured, and memory is "
                      "neither shown nor saved[/]")
    elif had_slot or not user_memory.snapshot():
        CONSOLE.print("  [dim]incognito off: capture and memory resumed[/]")
    else:
        CONSOLE.print("  [dim]incognito off: capture and saving resumed; "
                      "memory shows from the next chat[/]")
    return True


# One row per direct command; a `/word` with no row is not one.
_DIRECT: dict[str, Callable[..., bool]] = {
    "/embed": lambda args, *, _c="/embed", **_: _dc_refresh(args, _c),
    "/cooccur": lambda args, *, _c="/cooccur", **_: _dc_refresh(args, _c),
    "/lexical": lambda args, *, _c="/lexical", **_: _dc_refresh(args, _c),
    "/vault": _dc_vault,
    "/status": _dc_status,
    "/wiki": _dc_wiki,
    "/graph": _dc_graph,
    "/map": _dc_map,
    "/find": _dc_find,
    "/stale": _dc_stale,
    "/migrate": _dc_migrate,
    "/impact": _dc_impact,
    "/plans": _dc_plans,
    "/path": _dc_path,
    "/contested": _dc_contested,
    "/verify": _dc_verify,
    "/check": _dc_check,
    "/agenda": _dc_agenda,
    "/episodes": _dc_episodes,
    "/changes": _dc_changes,
    "/undo": _dc_undo,
    "/delete": _dc_delete,
    "/trash": _dc_trash,
    "/revert": _dc_revert,
    "/zotero": lambda args, **kw: _dc_zotero(args, **kw),
    "/mail": _dc_mail,
    "/connect": _dc_connect,
    "/review": _dc_review,
    "/anneal": _dc_anneal,
    "/curate": _dc_curate,
    "/aliases": _dc_aliases,
    "/keep": _dc_keep,
    "/remember": _dc_remember,
    "/memory": _dc_memory,
    "/incognito": _dc_incognito,
}


def _handle_direct_shortcut(raw_input: str, messages: list[dict], ask=None) -> bool:
    """Execute read-only commands directly without an LLM round-trip.

    Operates on the raw (case-preserved) input so that query strings and file
    paths reach the tool with their original casing intact.  Returns True if
    the command was handled, False to fall through to the normal dispatch.

    Handled commands (immediate, synchronous):
        /status [run_id]
        /embed [folder] [--force]
        /cooccur [folder] [--force]
        /lexical [folder] [--force]
        /graph [output.html] [folder]
        /map <note> [--force]
        /find <query> [--k=N]
        /impact [<git-range>]
        /path <noteA> <noteB>
        /contested
        /verify <note> [--clear]
        /changes
        /undo [note-path]
        /delete <path>
        /trash [restore <path> | erase <path> | empty]
    """

    parts = _direct_args(raw_input.strip())
    if not parts:
        return False
    cmd = parts[0].lower()

    handler = _DIRECT.get(cmd)
    if handler is None:
        return False
    # `messages` for /incognito, which hides the memory message of the
    # conversation in progress; every other handler ignores it.
    return handler(parts[1:], raw_input=raw_input, messages=messages, ask=ask)


def _chunk_by_json_size(items: list, max_bytes: int = 4000) -> list[list]:
    """Greedily pack items into chunks whose JSON size stays under max_bytes.
    Each chunk becomes one ledger task / one batch-tool call."""
    chunks: list[list] = []
    cur: list = []
    size = 0
    for it in items:
        s = len(json.dumps(it))
        if size + s > max_bytes and cur:
            chunks.append(cur)
            cur, size = [it], s
        else:
            cur.append(it)
            size += s
    if cur:
        chunks.append(cur)
    return chunks


def _seed_batch_ledger(cap: str, payloads: list[dict], *, kind: str, label: str) -> str:
    """Create a remediation Run whose tasks each invoke `cap` with one payload,
    emit the batch-start event, and return the agent-facing message. Shared by
    /enrich and /dedup — the async, resumable, progress-tracked path."""
    from pathlib import Path
    import orjson
    from silica.kernel.progress import PlanStep, Run
    from silica.ui.renderer import emit_batch_event
    from silica.obs.events import BatchRunStartEvent

    run = Run.new(
        mode="analyst",
        user_request=f"{kind} {label}",
        checkpoints=[PlanStep(id="remediate", kind="gate", objective=cap)],
        inputs={"scope": label},
    )
    for i, payload in enumerate(payloads):
        task = run.progress.add_task(cap)
        body = {**payload, "_reason": f"Batch {i + 1} of {len(payloads)}"}
        payload_path = str(run.payloads_dir / f"{task.id}.json")
        Path(payload_path).write_bytes(orjson.dumps(body, option=orjson.OPT_INDENT_2))
        task.input_ref = payload_path
    run.save()
    emit_batch_event(BatchRunStartEvent(run_id=run.run_id, kind=kind, label=label, total=len(payloads)))
    from silica.obs import narration as _narr_mod
    # Left running on purpose: a seeded ledger IS pending until executed, and
    # a projection showing it running is the truth, not a leak.
    _narr_mod.NARRATOR.span_open(
        "run", f"run-{run.run_id}", f"{kind} {label}: {len(payloads)} batch(es)",
        {"run_id": run.run_id, "kind": kind, "total": len(payloads)})
    return (
        f"A ledger for /{kind} has been created with {len(payloads)} chunk(s) "
        f"(run_id={run.run_id}). Use `silica_ledger_next` with this run_id to execute them."
    )


_WEB_USAGE = (
    "/web has nothing to search for. Usage: /web <keywords>, or a bare /web "
    "right after a question to answer that question from the web."
)


def _expand_web_turn(user_input: str, messages: list[dict]) -> tuple[str, str] | None:
    """`/web [keywords]` — the consent turn. Returns (question, instruction).

    None when the input is not `/web`. Raises ValueError (usage) when there are
    neither keywords nor a prior question to escalate.

    Deliberately NOT a direct handler: run_agent appends the assistant and tool
    turns to the shared `messages` itself, so a handler running its own loop and
    then reporting the answer would append that answer a second time — the GUI's
    generic direct-command wrapper renders it as a fenced text block, which is
    how a web answer would arrive both as markdown and as a code block.
    """
    parts = user_input.strip().split()
    if not parts or parts[0].lower() != "/web":
        return None  # "/web-search" is its own command and must not match here
    question = " ".join(parts[1:]).strip()
    if not question:
        # Bare /web: the question is already in the history, no pending state
        # needed. `origin` marks CLI-expanded directives — re-asking one of those
        # on the web would escalate a harness instruction, not a human question.
        question = next(
            (
                m["content"] for m in reversed(messages)
                if m.get("role") == "user" and not m.get("origin") and m.get("content")
            ),
            "",
        )
        if not question:
            raise ValueError(_WEB_USAGE)
    from silica.sources.web_research import web_turn_instruction

    return question, web_turn_instruction(question)


# --- shortcut handlers -------------------------------------------------------
# One per command, module-level and independently readable: the dispatcher below
# is a table, not a 20-branch chain, and `git log -L` on one command no longer
# spans every other. Each takes the tokens AFTER the command word. The two extras
# (`user_input`, `progress`) are passed to every handler and absorbed by `**_`, so
# a handler's signature names exactly what it reads.


def _sc_nucleate(args: list[str], *, user_input: str = "", ask=None, **_) -> str | None:
    """/nucleate <file|folder|glob...> [--target=DIR] [--hub=H] [--profile=P]
    [--seen=YYYY-MM-DD] [--no-keep-sources] — ingest sources into vault notes.

    No arguments drains the write-ahead log instead. Fully inline: returns "" when
    it dispatched, or an agent-directed message when the target cannot be resolved
    from the arguments alone.
    """
    args = ingest.rejoin_spaced_paths(args)
    if not args:
        return ingest.drain_wal(CONSOLE)
    seen = _str_flag(args, "--seen")  # capture clock: the day the events happened
    if seen:
        # Trust boundary: this string becomes the valid_from on every claim
        # of the run — a typo'd date would poison note_clock vault-wide.
        import datetime as _dt
        try:
            seen = _dt.date.fromisoformat(seen).isoformat()
        except ValueError:
            CONSOLE.print(f"  [yellow]--seen ignored: {seen!r} is not YYYY-MM-DD[/]")
            seen = ""
    return ingest.nucleate(
        _positional(args),  # preserve original case
        target_dir=_str_flag(args, "--target"),
        hub=_str_flag(args, "--hub"),
        seen=seen,
        # Explicit lens override: tops the whole form ladder
        # (docs/specs/nucleation-forms.md), filing included.
        profile=_str_flag(args, "--profile"),
        # On by default: the leaf lives in `sources/`, which every index built on
        # the vault listing leaves out (is_source_leaf: embeddings, the graph, the
        # driver's search), while the passage search behind silica_search_context
        # walks it and returns it (kernel/recall/paths.py:235, probed 2026-11-04).
        # It is what makes a note's verbatim source reachable at all
        # (reliability_tier reads exactly that). --no-keep-sources opts out.
        keep_sources="--no-keep-sources" not in args,
        user_input=user_input,
        console=CONSOLE,
        ask=ask,
    )


def _sc_settings(args: list[str], **_) -> str | None:
    """/settings [<key> <value|none>] — read or edit vault.yaml without the wizard."""
    # View/edit vault.yaml without the wizard. ponytail: safe_dump rewrite —
    # YAML comments are not preserved; hand-edit the file to keep them.
    import yaml as _yaml
    from pathlib import Path as _P
    from silica.kernel.base.manifest import MANIFEST_REL, reset_manifest_cache
    _KEYS = {"cooccurrence_lang", "conventions.language",
             "conventions.reply_language", "conventions.max_tags"}
    mf = _P(CONFIG.vault_path) / MANIFEST_REL
    data = _yaml.safe_load(mf.read_text(encoding="utf-8")) if mf.exists() else {}
    if not isinstance(data, dict):
        data = {}
    if not args:
        CONSOLE.print(f"  [bold]{mf}[/]")
        body = _yaml.safe_dump(data, allow_unicode=True, sort_keys=False).rstrip() \
            if data else "(defaults — no vault.yaml yet)"
        CONSOLE.print(f"  {body}")
        CONSOLE.print(f"  Keys: {', '.join(sorted(_KEYS))} — /settings <key> <value|none>")
        return ""
    if len(args) != 2 or args[0] not in _KEYS:
        return f"Error: usage /settings <key> <value|none>. Keys: {', '.join(sorted(_KEYS))}"
    key, raw = args
    val = None if raw.lower() in ("none", "null") else (int(raw) if raw.isdigit() else raw)
    node = data
    *heads, leaf = key.split(".")
    for h in heads:
        if not isinstance(node.get(h), dict):
            node[h] = {}
        node = node[h]
    if val is None:
        node.pop(leaf, None)
    else:
        node[leaf] = val
    # Atomic: a torn vault.yaml parses as defaults, and default write_dir=""
    # is the whole vault root — the exact boundary this file exists to set.
    from silica.kernel.base.durable import atomic_write_bytes
    atomic_write_bytes(mf, _yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False).encode("utf-8"))
    reset_manifest_cache()
    CONSOLE.print(f"  {key} = {raw} → {mf.name} (comments in the file are not preserved)")
    return ""


def _sc_convert(args: list[str], **_) -> str | None:
    """/convert <file...> [--target=DIR] — document to inbox notes, no distillation."""
    args = ingest.rejoin_spaced_paths(args)
    files = _positional(args)
    target_dir = _str_flag(args, "--target")
    if not files:
        return "Error: /convert requires at least one file path. Usage: /convert <file...> [--target=DIR]"
    from silica.sources.convert import convert
    for f in files:
        try:
            paths = convert(f, dest_dir=target_dir)
            CONSOLE.print(
                f"  Converted {f} → [bold]{len(paths)}[/] note(s): {', '.join(paths)}"
            )
        except ValueError as e:
            CONSOLE.print(f"  [yellow]Skipped {f}: {e}[/]")
    return ""  # fully handled inline — sentinel: nothing for the agent


def _sc_web_search(args: list[str], *, progress=None, **_) -> str | None:
    """/web-search "<concept>" [--max-searches=N] — research the web into an inbox note."""
    from rich.markup import escape

    from silica.sources.web_research import web_research, _DEFAULT_MAX_SEARCHES
    max_searches = _int_flag(args, "--max-searches=", _DEFAULT_MAX_SEARCHES)
    concept = " ".join(_positional(args)).strip()
    if not concept:
        return 'Error: /web-search requires a concept. Usage: /web-search "<concept>" [--max-searches=N]'

    # The REPL's renderer, never a second one: __init__ overwrites the
    # module-global batch hook and only close() clears it, so a throwaway
    # renderer left the global pointing at an orphan and /enrich
    # and /dedup silently lost their batch panel for the rest of the
    # session. Own one only when there is nothing to reuse, and close it.
    renderer = progress
    if renderer is None:
        from silica.ui.renderer import make_progress_callback
        renderer = make_progress_callback()
    try:
        note_rel = web_research(
            concept, max_searches=max_searches,
            tool_progress_callback=renderer,
        )
        # escape(), not markup=False: a note title Rich reads as a tag gets
        # silently eaten (the user is told a path that is not the file's
        # name), and a URL carrying `[/x]` raises MarkupError straight out
        # of the except that exists to report the failure. Escaping only the
        # interpolated value keeps the styling on the rest of the line.
        CONSOLE.print(
            f"  Findings → [bold]{escape(note_rel)}[/]"
            "  (review, then /nucleate to bring it in)"
        )
    except Exception as e:  # missing key, no findings, convergence guard, network
        CONSOLE.print(f"  [yellow]web-search failed: {escape(str(e))}[/]")
    finally:
        if progress is None:
            renderer.close()
    return ""  # fully handled inline — sentinel: nothing for the agent


def _sc_fetch(args: list[str], **_) -> str | None:
    """/fetch <url> — one page (or video transcript) into an inbox note."""
    from rich.markup import escape

    from silica.sources.web_research import fetch_to_inbox
    url = " ".join(args).strip()
    if not url:
        return "Error: /fetch requires a URL. Usage: /fetch <url>"

    try:
        note_rel = fetch_to_inbox(url)
        CONSOLE.print(
            f"  Fetched → [bold]{escape(note_rel)}[/]"
            "  (review, then /nucleate to bring it in)"
        )
    except Exception as e:  # SSRF guard, bot wall, missing yt-dlp, network
        CONSOLE.print(f"  [yellow]fetch failed: {escape(str(e))}[/]")
    return ""  # fully handled inline — sentinel: nothing for the agent


def _sc_report(args: list[str], **_) -> str | None:
    """/report [folder] [--top-k=N] [--embeddings] [--cooccurrence] — structural audit."""
    folder = ""
    top_k = _int_flag(args, "--top-k=", 10)
    with_embeddings = False
    # Off by default like --embeddings: the co-occurrence delta runs a
    # per-note expanded ranking, the report's other expensive pass. Without
    # it stale links and missing hubs are never computed at all, so the
    # escalate rules that read them can never fire.
    with_cooccurrence = False

    for arg in args:
        if arg.startswith("--folder="):
            folder = arg[len("--folder="):]
        elif arg in ("--embeddings", "--with-embeddings"):
            with_embeddings = True
        elif arg in ("--cooccurrence", "--with-cooccurrence"):
            with_cooccurrence = True
        elif not arg.startswith("-"):
            folder = arg  # positional: /report Concepts/ML

    scope_desc = f"scoped to `{folder}`" if folder else "on the whole vault"
    embed_note = " Also propose missing links via the embedding index." if with_embeddings else ""
    if with_cooccurrence:
        embed_note += (" Also read the co-occurrence delta: autolink candidates, stale links"
                       " and missing hubs.")

    return (
        f"Run a structural vault audit {scope_desc}.{embed_note}\n"
        f"Call `silica_vault_report` with "
        f"folder={json.dumps(folder)}, top_k={top_k}, "
        f"with_embeddings={'true' if with_embeddings else 'false'}, "
        f"with_cooccurrence={'true' if with_cooccurrence else 'false'}.\n"
        f"Then STOP. Write a short, human-readable brief in chat from the returned `digest` "
        f"(totals, top hubs, and how many fixes are available: auto / propose / issues), and "
        f"point the user to the GRAPH_REPORT.md that was written.\n"
        f"Do NOT run the steering loop, do NOT call `silica_ledger_next`, and do NOT apply any "
        f"autolinks, corrections, renames, or deletions. Instead, ask the user whether they want "
        f"to apply the changes. Only if they explicitly say yes, resume the run (`run_id`) and "
        f"follow the steering loop."
    )


def _sc_enrich(args: list[str], **_) -> str | None:
    """/enrich [folder] — seed a batch ledger over a folder's notes."""
    from silica.driver import DRIVER
    from silica.kernel.base.layout import in_folder

    folder = next(iter(_positional(args)), "")

    # list_files(folder) pre-filters loosely (startswith); in_folder tightens
    # it so /enrich Foo never leaks into a sibling FooBar/ folder.
    paths = [r.path for r in DRIVER.list_files(folder=folder) if in_folder(r.path, folder)]
    if not paths:
        return f"Error: no files found in '{folder}'."

    payloads = [{"note_paths": chunk} for chunk in _chunk_by_json_size(paths)]
    return _seed_batch_ledger("silica_enrich_batch", payloads, kind="enrich", label=folder or "vault")


def _sc_dedup(args: list[str], **_) -> str | None:
    """/dedup [folder] — seed a batch ledger over the near-duplicate pairs found."""
    from silica.kernel.recall import duplicates

    folder = " ".join(_positional(args))

    pairs, err = duplicates.scan_pairs(folder)
    if err:
        CONSOLE.print(f"  [yellow]{err}[/]")
        return ""  # handled inline — nothing for the agent
    if not pairs:
        CONSOLE.print(f"  No near-duplicate pairs in [bold]{folder or '(vault)'}[/].")
        return ""

    payloads = [{"pairs": chunk} for chunk in _chunk_by_json_size(pairs)]
    return _seed_batch_ledger("silica_dedup_pairs", payloads, kind="dedup", label=folder or "vault")


def _sc_organize(args: list[str], **_) -> str | None:
    """/organize [intent] [--scope=DIR] [--file=TAXONOMY] [--apply] [--merge]
    [--move-uncategorized] — agent-directed taxonomy build and move plan."""
    intent_parts = _positional(args)
    scope = _str_flag(args, "--scope")
    taxonomy_file = _str_flag(args, "--file")
    apply_now = "--apply" in args
    merge = "--merge" in args
    move_uncat = "--move-uncategorized" in args

    # Both organizer tools filter with `ref.path.startswith(scope)` over
    # vault-relative paths, so an absolute --scope matches zero notes and the
    # whole run reports success on an empty plan. Normalize here, where the
    # user types the path, and refuse a scope outside the vault out loud.
    if scope:
        from silica.kernel.base.layout import to_vault_relative
        try:
            scope = to_vault_relative(scope, ensure_md=False)
        except ValueError as exc:
            return f"Error: /organize --scope is not usable: {exc}"

    # Re-join intent (handles both quoted and unquoted multi-word)
    intent = " ".join(intent_parts).strip('"\'')
    run_extra = ", move_uncategorized=true" if move_uncat else ""

    if taxonomy_file:
        # Skip taxonomy generation — use existing file
        dry = "false" if apply_now else "true"
        scope_str = f", scope={json.dumps(scope)}" if scope else ""
        msg = (
            f"Run the vault organizer using the existing taxonomy file {json.dumps(taxonomy_file)}.\n"
            f"Call `silica_run_organizer` with taxonomy_path={json.dumps(taxonomy_file)}{scope_str}, "
            f"dry_run={dry}{run_extra}.\n"
        )
        if not apply_now:
            msg += (
                "Show the move plan to the user and ask for confirmation. "
                "If confirmed, call `silica_run_organizer` again with dry_run=false."
            )
    elif intent:
        scope_str = f", scope={json.dumps(scope)}" if scope else ""
        merge_str = ", merge=true" if merge else ""
        dry_note = (
            f"Then call `silica_run_organizer` with dry_run=true{run_extra} to preview the moves. "
            "Show the plan to the user and ask for confirmation before executing."
        ) if not apply_now else (
            f"Then call `silica_run_organizer` with dry_run=false{run_extra} to execute the moves."
        )
        msg = (
            f"Organize the vault based on the user's intent: {json.dumps(intent)}.\n"
            f"Step 1: Call `silica_generate_taxonomy` with user_intent={json.dumps(intent)}{scope_str}{merge_str}.\n"
            f"Step 2: Show the generated taxonomy to the user and ask if it looks correct.\n"
            f"Step 3: {dry_note}"
        )
    else:
        msg = (
            "Help me organize my vault. "
            "Ask me to describe how I want to group my notes, "
            "then call `silica_generate_taxonomy` with my answer, "
            "show me the taxonomy, and run `silica_run_organizer` with dry_run=true to preview."
        )
    return msg

# --- reader commands: agent-directed, strictly read-only ---------------------
# /summarize /explain /compare /relate /schematize /diagram are skills now
# (silica_core/builtin_skills, ADR-0063). These two stay Python because the
# Python decides: /quiz picks one of three instruction sets and computes its
# count from --focus, /learn reads the ledger and the plan notes.


# A study artifact from the notes, beside /quiz: the deck and the cards
# NotebookLM's Studio makes (owner's comparison, 2026-10-23), both drawn from
# Learn's checked key points (learn.artifacts.study_points). The cards are made
# as data and checked before they are shown (learn.artifacts.flashcards); the
# deck is written by the agent and its points are checked after it.


def _sc_flashcards(args: list[str], **_) -> str | None:
    """/flashcards <note|folder> [--n=12] [--save=<path>]: question-and-answer
    cards for review outside a session, one checked point each."""
    from silica.kernel.learn.artifacts import flashcards

    targets = _positional(args)
    if not targets:
        return "Error: /flashcards needs a note or a folder. Usage: /flashcards <note|folder> [--n=12] [--save=<path>]"
    CONSOLE.print(FlatMarkdown(flashcards(targets, _int_flag(args, "--n=", 12), _str_flag(args, "--save").strip())))
    return ""  # fully handled inline: nothing for the agent


def _sc_slides(args: list[str], **_) -> str | None:
    """/slides <note|folder> [--save=<path>]: a presentation of what the notes
    say, in the Markdown Obsidian's Slides plugin presents."""
    from silica.kernel.learn.artifacts import study_points

    save = _str_flag(args, "--save").strip()
    targets = _positional(args)
    if not targets:
        return "Error: /slides needs a note or a folder. Usage: /slides <note|folder> [--save=<path>]"
    titles, points = study_points(targets)
    ground = ("Build the slides from these points, each already checked against its source, in this order "
              "unless the topic's logic asks another:\n" + "\n".join(points) if points else
              "Read the note(s) first (list a folder's notes; a lesson's outline note gives the order).")
    out = ("Then write the deck, unchanged, as the note `" + save + "` with silica_write_note, so Obsidian's "
           "Slides plugin presents it."
           if save else "READ-ONLY: do not create, edit, patch, or move any note.")
    # No example embed in the format line: given one, the local 9B put
    # `![[file.png]]` on every slide of a lecture deck (2026-10-23).
    return (
        f"Make a slide deck from {', '.join(f'[[{t}]]' for t in titles) or ', '.join(targets)}.\n"
        f"{ground}\n"
        f"Format: Markdown slides separated by a line holding only `---`. Slide 1 is the title and one line on "
        f"what the deck covers; then 6 to 10 slides, each a `## ` title and 3 to 5 short bullets in the notes' "
        f"own words, formulas kept as written in $...$; a figure only where a note embeds one, its embed line "
        f"copied exactly from the note, and no figure line otherwise; the last slide is three questions the "
        f"reader should now be able to answer. "
        f"Cite each slide's source note as a [[wikilink]] on its last line.\n"
        f"Answer in chat with the deck in one ```markdown block. {out}"
    )


def _sc_quiz(args: list[str], **_) -> str | None:
    """/quiz [note|folder] [--n=10] [--focus="passage"]: an agent-directed
    active-recall round; --focus narrows it to one passage of the note."""
    focus = _str_flag(args, "--focus").strip()
    # A passage holds a few testable claims, not ten: ten questions on one
    # paragraph would be asked by rewording the same sentence.
    n = _int_flag(args, "--n=", 3 if focus else 10)
    targets = _positional(args)
    if targets and focus:
        # The drawer's selection bar (ADR-0059): the round stays a round on the
        # NOTE, so the ledger lines still carry its path, only the questions
        # are drawn from the passage.
        source = "from `" + targets[0] + "`, on this passage of it only:\n> " + focus.replace("\n", "\n> ")
        pick = ("Read the note for the context the passage sits in, but ask only what the "
                "passage itself states.")
    elif targets:
        source = "from " + ", ".join(f"`{t}`" for t in targets)
        pick = "Read the note(s) (list a folder's notes first)."
    else:
        # No target: the learner model picks. Half the round re-tests what
        # is decaying, half probes what was never measured — every graded
        # answer feeds the same ledger either way (spec: learner-model).
        source = "from the learner model's review queue"
        pick = (
            "Call silica_review_queue to pick the targets, and read them. Mix both pools: "
            "'due' rows were known once and are decaying — re-test them; 'unexplored' rows "
            "were never measured, and the AI-written ones were never learned at all, so "
            "probe those first. An empty queue means an empty vault: say so."
        )
    return (
        f"Run a {n}-question active-recall quiz {source}.\n"
        f"{pick}\n"
        f"Mix recall, comprehension, and application questions; ask only what the notes "
        f"actually support.\n"
        f"Ask the numbered questions and STOP. Do not reveal the answers in the same "
        f"message: retrieving from memory is what makes the round worth running, and a "
        f"visible answer key destroys it.\n"
        f"When the reader replies, grade each answer, cite each source note as a "
        f"[[wikilink]], then call silica_record_quiz once with one entry per question: "
        f"path, correct, concepts (the 1-3 concepts that question tested), q (the "
        f"question text), and anchor ('#Heading' when one heading clearly holds the "
        f"answer). Grade an unanswered or skipped question as incorrect.\n"
        f"After grading, offer ONE follow-up round drilling what was just missed — run "
        f"it only if the reader accepts.\n"
        f"A wrong answer is the reader's miss, not the note's fault, and needs nothing "
        f"beyond the grade. If grading instead exposes a fault in the note itself (it "
        f"states something wrong, or contradicts another note you read), offer to record "
        f"that with silica_flag_note, and only run it once the reader says so.\n"
        f"READ-ONLY apart from that flag: do not create, edit, patch, or move any note."
    )


def _kv_flag(args: list[str], flag: str) -> str | None:
    """`--flag value` or `--flag=value`; None when absent, "" for a bare flag."""
    for i, a in enumerate(args):
        if a == flag:
            nxt = args[i + 1] if i + 1 < len(args) else ""
            return "" if nxt.startswith("--") else nxt
        if a.startswith(flag + "="):
            return a[len(flag) + 1:]
    return None


_LEARN_SESSION_RULES = (
    "For each item, in order:\n"
    "1. Ask ONE item per message: the note title and the question, nothing else: no hint, "
    "no note body, and never the answer or the claim wording. STOP and wait.\n"
    "2. When the reader answers, ask for confidence BEFORE any feedback, with exactly these "
    "labels: 90 / 70 / 50 / 25, or Skip (records null). Never infer it; if the reader stated "
    "one already, do not ask twice.\n"
    "3. Hints only on request, at most two, taken from the item's `hints` list in order. "
    "\"Just tell me\" is honored at once: show the answer, say \"Answer shown; independent "
    "recall was not checked.\", and log told=true (Hard at best).\n"
    "4. Grade blind against the source: read `source.leaf` with silica_read_note and quote "
    "the line at `source.line` (the span) beside the note's claim. Rubric: list what is "
    "missing before what is present; meaning over wording; a why question owes a mechanism "
    "(the what alone is partial); fluency is not evidence; when torn, round down and quote "
    "the criterion; an empty answer is lapsed. Outcome recalled/partial/lapsed maps to rating "
    "good/hard/again; easy only when the answer was complete and precise with no hint and "
    "confidence 70 or more. Caps after the rubric: any hint or told caps at Hard; a correct "
    "answer at confidence 25 caps at Good. Show the proposed rating (Again/Hard/Good/Easy) "
    "and accept the reader's change; log both when they differ. If the note's claim is NOT "
    "supported by the span, say \"Source mismatch\", show both, offer silica_flag_note (run "
    "it only on a yes) and record NOTHING for that item.\n"
    "5. Record with silica_record_quiz, one entry per item: path, correct (the rubric met, "
    "before caps), rating, kind, level, q, anchor, concepts, conf, source {leaf, line}, grader "
    "(\"model\", \"self\" or \"told\"), grader_rating when the reader overrode yours, hints "
    "(rungs used), told, objective and point as the item carries them, session (the id "
    "silica_learn_items returned) and plan when one.\n"
    "6. One momentum line only on a milestone (first success on a note, estimated hold "
    "crossing 7 or 30 days, or doubling); never on Hard or Again, never \"keep it up\". "
    "Label an unsuccessful predict item \"First attempt\", not a lapse. Then offer the next "
    "item and STOP.\n"
    "Plain sentences, no praise padding. Question and feedback in the material's language.\n"
    "READ-ONLY apart from silica_record_quiz and a flag the reader asks for."
)


def _learn_session_prompt(n: int, target: str = "") -> str:
    call = f"silica_learn_items(n={n}" + (f", target={json.dumps(target)}" if target else "") + ")"
    where = f" inside the plan `{target}` at its frontier" if target else " from the whole vault"
    return (
        f"Run one study session of n={n} items{where}.\n"
        f"Call {call} once. Relay its `amnesty` and `short` lines verbatim when present; an "
        f"empty item list means the vault has no grounded material in scope: say what is "
        f"missing and stop.\n" + _LEARN_SESSION_RULES
    )


def _learn_plan_prompt(name: str, kind: str, target: str, restrict: str = "") -> str:
    """The syllabus writer (D17, D23, D24): one plan note through the write gate."""
    props = {"type": "plan", "scope": kind, "target": target, "reach": "relational"}
    if restrict:
        props["restrict"] = restrict
    read = {
        "folder": (
            f"Call silica_review_queue(target={json.dumps(target)}): it returns every note "
            f"under the prefix with its R, already in prerequisite order (topological over the "
            f"vault's directed relation). Trust that order; do not re-derive prerequisites "
            f"from prose."
        ),
        "notes": f"Read each of these notes: {target}.",
        "topic": (
            f"Call silica_concepts(term={json.dumps(target)}) for the notes carrying the term "
            f"and its neighbour concepts; read the MOC that names it when one exists."
        ),
        "steps": (
            f"Read `{target}` (a syllabus, a course program, or the directive as typed). "
            f"Resolve EACH step line to the vault notes that cover it: title first, then "
            f"silica_search_context; a step with no covering note is KEPT and marked "
            f"\"not in the vault\" (the plan's most useful line, never dropped)."
            + (f" Restrict resolution to notes under `{restrict}`." if restrict else "")
            + " Then call silica_review_queue(target=...) on the covered area for the order "
            "and the retention state; it is already in prerequisite order, trust it."
        ),
    }[kind]
    steps_rule = (
        "`## Steps`, one `- [ ]` line per step in that order, each with a because clause: "
        "`- [ ] [[Softmax]] before [[Cross-entropy]]: because the loss is defined on the "
        "softmax output`; a gap reads `- [ ] <step>: not in the vault`. Start at the "
        "frontier: skip steps whose notes still hold (high R) and say so in one line."
        if kind == "steps" else
        "no `## Steps` section (the scope orders itself)."
    )
    return (
        f"Write a study plan named `{name}` over the scope {kind}={json.dumps(target)}"
        + (f" restricted to `{restrict}`" if restrict else "") + ".\n"
        f"1. {read}\n"
        f"2. Write ONE plan note via silica_write_note(path=\"Plans/{name}.md\", "
        f"props={json.dumps(props)}) with body: `# {name}`; one plain sentence naming the "
        f"goal; `## Tree` (children as bullets, leaves as [[wikilinks]], a note under several "
        f"children listed once per child); {steps_rule} End with an empty `## Sessions`. "
        f"Nothing about mastery goes in the note: every reading derives from the ledger.\n"
        f"3. Then STOP and ask whether to begin; `/memorize {name}` continues the plan at its "
        f"frontier.\n"
        f"A `type: syllabus` note from before is read as a steps plan; never rewrite one, "
        f"continue it."
    )


def _learn_teach_prompt(note: str) -> str:
    return (
        f"Teach-back on `{note}` (learn-suite D15, kind teach).\n"
        f"Phase 1, four turns: play a curious student who sees ONLY the note's title and its "
        f"concepts (silica_concepts(note={json.dumps(note)})) and never the body, the source "
        f"or the vault. Ask the reader to explain `{note}` to you; on each reply ask ONE naive "
        f"follow-up about what stayed unclear; STOP after every question and wait. No grading, "
        f"no hints, no corrections during the four turns.\n"
        f"Phase 2, the summary: now read the note and its source leaf with silica_read_note. "
        f"Write a summary that checks the reader's explanation for connected propositions "
        f"against the claims (cause, effect, condition, the relation between parts), never "
        f"paraphrase quality or connective counts; quote the span for anything missed. Propose "
        f"a rating (again/hard/good/easy) and accept the reader's change. Record ONE "
        f"silica_record_quiz entry: path, correct, rating, kind \"teach\", level 3, q, conf "
        f"null, source {{leaf, line}}, grader \"model\", grader_rating when overridden, "
        f"session (a fresh uuid).\n"
        f"Plain sentences, no praise padding. READ-ONLY apart from that record."
    )


def _sc_learn(args: list[str], **_) -> str | None:
    """/learn v2 (learn-suite D20): a session, a plan to continue, a plan to
    write, a teach-back, or the readings. One door, no new command name."""
    n = _int_flag(args, "--n=", 8)
    status = _kv_flag(args, "--status")
    if status is not None:
        from silica.kernel.learn import mastery

        line = mastery.status_line(status)
        return f"Relay this status line to the reader verbatim, then stop:\n{line}"
    teach = _kv_flag(args, "--teach")
    if teach is not None:
        if not teach:
            return "Error: /memorize --teach requires a note. Usage: /memorize --teach <note>"
        return _learn_teach_prompt(teach)
    plan = _kv_flag(args, "--plan")
    scope = _str_flag(args, "--scope")
    steps = _str_flag(args, "--steps")
    if plan is not None:
        if not plan:
            return "Error: /memorize --plan requires a name. Usage: /memorize --plan <name> --scope=<folder|note,note|topic:term> [--steps=<note>]"
        if steps:
            return _learn_plan_prompt(plan, "steps", steps, restrict=scope)
        if not scope:
            return "Error: /memorize --plan needs --scope=<folder|note,note|topic:term> or --steps=<note>"
        if scope.startswith("topic:"):
            return _learn_plan_prompt(plan, "topic", scope[len("topic:"):])
        if "," in scope or (not scope.endswith("/") and scope.endswith(".md")):
            return _learn_plan_prompt(plan, "notes", scope)
        return _learn_plan_prompt(plan, "folder", scope)
    targets = [a for a in _positional(args) if not a.startswith("--")]
    if not targets:
        return _learn_session_prompt(n)
    target = " ".join(targets)
    # A plan by that name continues at its frontier; anything else is the v1
    # syllabus flow, a steps plan written over that scope (D17). The vault
    # is the registry, so the plan lookup is a frontmatter scan, not a store.
    # New-material targets (learner-model extension 1) plug in HERE: resolve
    # `target` against outside sources before the plan is written.
    try:
        from silica.kernel.learn import plans

        found = plans.find_plan(target)
    except Exception:
        found = None
    if found is not None:
        return _learn_session_prompt(n, target=found["path"])
    return _learn_plan_prompt(Path(target.rstrip("/")).name or target, "steps", target)


def _sc_remember(args: list[str], **_) -> str | None:
    """Bare `/remember`: the user asks the model to keep what was just said.
    `/remember <text>` never reaches here (_dc_remember writes it verbatim).
    The model's save still passes the taint gate (ADR-0076 §5)."""
    from silica.tools.user_memory import REMEMBER_DIRECTIVE

    if args:
        return None
    return (
        f"{REMEMBER_DIRECTIVE} Save it with silica_remember (action save): one short "
        "self-contained line, scope user for what is true of the person in every vault, "
        "scope vault for the state of this work. If nothing in the conversation is clearly "
        "what they mean, ask which fact to keep instead of guessing. Reply with what you saved."
    )


# The dispatch table IS the list of Python shortcuts: one row per command. A
# `/word` with no row here is tried as a skill (ADR-0063), and failing that it
# goes to the agent as prose.
_SHORTCUTS: dict[str, Callable[..., str | None]] = {
    "/promote": lambda args, **_: ingest.promote(_positional(args), CONSOLE),
    "/nucleate": _sc_nucleate,
    "/settings": _sc_settings,
    "/convert": _sc_convert,
    "/web-search": _sc_web_search,
    "/fetch": _sc_fetch,
    "/report": _sc_report,
    "/enrich": _sc_enrich,
    "/dedup": _sc_dedup,
    "/organize": _sc_organize,
    "/quiz": _sc_quiz,
    "/flashcards": _sc_flashcards,
    "/slides": _sc_slides,
    "/memorize": _sc_learn,
    # the command's name until ADR-0141 named the view Memorize; typed from habit, it still runs
    "/learn": _sc_learn,
    "/remember": _sc_remember,
}


def _expand_skill(user_input: str) -> str | None:
    """`/<name> <args>` through the skill of that name, None when there is none.

    The arguments go over as typed, never through shlex here: the skill's own
    placeholders decide how they are read, and an apostrophe in prose
    (/explain dell'entropia) must not be a quoting error.
    """
    from silica_core.kernel import skills

    cmd, *rest = user_input.strip().split(None, 1)
    found = skills.discover(CONFIG.vault_path or None)
    skill = found.get(cmd[1:].lower())
    if skill is None:
        return None
    args = rest[0] if rest else ""
    try:
        body = skills.expand(skill, args)
    except ValueError as e:  # the usage line: shown, and the model is never asked
        return f"Error: {e}"
    if skill.name == "diagram" and skill.source == "builtin" and (CONFIG.model or "").startswith("custom/"):
        # ADR-0082 gate 2: Qwen3.5-9B's /diagram parsed 46 of 48 once quoted,
        # but 5 of 10 audited carried arrows its note does not hold (a list
        # chained into a sequence that never happens), and a wrong diagram
        # teaches worse than none. A local model answers with the table
        # /schematize writes. ponytail: keyed on the route, so a large model
        # served locally gets the table too; rerun evals/probe_chat_figures.py
        # on it and exempt its id here if it passes.
        if (table := found.get("schematize")) is not None:
            return (skills.expand(table, args) + "\nThe reader asked for /diagram: say in the "
                    "caption that a table stands in for the diagram.")
    return body


def _expand_workflow_shortcut(user_input: str, progress=None, ask=None) -> str | None:
    """Expand workflow shortcuts (e.g. /report, /nucleate) into agent-directed messages.

    Returns the expanded message string, or None if the input is not a
    recognised shortcut. Expanded messages flow through the normal agentic
    loop so the agent calls the tools and follows the steering protocol.

    `progress` is the REPL's own renderer, for the shortcuts that drive work
    inline. A `_ProgressRenderer` claims the module-global batch hook and
    subscribes to the BUS for its whole life, so a second one built here would
    orphan the REPL's — reuse it when the caller has one.

    `ask` is the surface's way to put a question to the person at it, None when
    there is nobody to ask. The surface says so, because the process cannot:
    `silica --gui` from a shell has a tty on stdin and the person in a browser.
    """
    if not user_input.strip().startswith("/"):
        return None  # not a shortcut — skip shlex entirely, plain prose can have stray quotes/apostrophes
    cmd = user_input.split(None, 1)[0].lower()
    handler = _SHORTCUTS.get(cmd)
    if handler is None:
        return _expand_skill(user_input)
    try:
        parts = shlex.split(user_input.strip())  # honours quoted paths with spaces
    except ValueError:
        return "Error: unbalanced quotes in command. Wrap paths with spaces in \"...\"."
    return handler(parts[1:], user_input=user_input, progress=progress, ask=ask)


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
        messages[:] = _fresh_messages() + replayed
        # The persona its last turn wore (ADR-0127 point 3), as the GUI's
        # /session/load puts it back.
        from silica.kernel import personas

        last: dict = next((m for m in reversed(replayed) if m.get("role") == "user"), {})
        if (worn := personas.available(CONFIG.vault_path or None).get(str(last.get(personas.TURN_KEY) or ""))):
            personas.apply(messages, worn)
        _update_context_tokens(messages)
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


def _resolve_context_budget() -> None:
    """Size the REPL context meter to the live model's window.

    SILICA_MAX_CONTEXT, when set, is the window on every provider. Locally it
    stands in for a served window silica can't reliably introspect (LM Studio,
    Ollama). On OpenRouter it is a price decision, a laptop's as much as the
    host's: a 32k cap cost 62% less a chat than the 1M window at equal answers
    (docs/evaluation/context-cap-2026-10-13.md). Until 2026-10-27 a laptop's
    pin was ignored on OpenRouter, so a 65536 in ~/.silica/.env ran DeepSeek
    at 1M. Falls back to the static default when the provider is unreachable.
    """
    if not _model_configured():
        return
    pinned = bool(env_raw("SILICA_MAX_CONTEXT"))
    if pinned and CONFIG.provider != "openrouter":
        return
    from silica.model.limits import model_limits
    window, _ = model_limits(CONFIG.provider, CONFIG.model)
    if window:
        # OpenRouter's window is the one it serves, so a pin left from a larger
        # model is cut to it instead of sending prompts the provider refuses.
        CONFIG.max_context_tokens = min(window, CONFIG.max_context_tokens) if pinned else window


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
        target = next(iter(_positional(args[1:])), "")
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
        _resolve_context_budget()
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
                from silica.kernel.calendar.reminders import (
                    advance_marks, delivery_lock, due_reminders, load_marks,
                    save_marks,
                )
                events = scan_events(vault)
                if not events:
                    continue
                with delivery_lock(vault):
                    marks = load_marks(vault)
                    due = due_reminders(events, marks, _dt.datetime.now())
                    if due:
                        save_marks(vault, advance_marks(marks, due))
                if not due:
                    continue
                for r in due:
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
    _resolve_context_budget()
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
    messages = _fresh_messages()
    collapsed: set[int] = set()  # message indices already elided by compaction
    # Number tokens of every result this session read, raw (ADR-0095): compaction
    # and a relevance verdict stub the tool messages, and must not decide what grounds.
    seen_numbers: set[str] = set()
    # This session's own identity, for the capture lane. Random, not a clock:
    # two silica processes started in the same second share the same vault WAL,
    # and a deterministic name would have one overwrite the other's envelope.
    # capture_session is opt-in and fail-open in itself, so both call sites
    # below are the bare call — no wrapper, nothing to guard.
    from silica.capture import capture_session
    session_id = uuid.uuid4().hex[:12]

    from silica.ui.renderer import make_progress_callback
    callback = make_progress_callback()
    # This surface owns the terminal, so here and only here its tty answers
    # whether a person can be asked: piped input has nobody to type a reply.
    ask = CONSOLE.input if sys.stdin.isatty() else None

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

        # A persona is worn, not run (ADR-0127 point 3): it edits this
        # conversation's system line; words after its name are this turn.
        if user_input.startswith("/"):
            from silica.kernel import personas

            worn = personas.command(user_input, messages, CONFIG.vault_path or None, not collapsed)
            if worn is not None:
                CONSOLE.print(f"  {worn[0]}")
                if not worn[1]:
                    continue
                user_input = typed = worn[1]

        # Direct shortcuts bypass the LLM entirely (case-sensitive args preserved)
        if user_input.startswith("/") and _handle_direct_shortcut(user_input, messages, ask=ask):
            continue

        # Expand workflow shortcuts (/report, /nucleate etc.) into agent-directed messages
        is_directive = False
        try:
            expanded = _expand_workflow_shortcut(user_input, progress=callback, ask=ask)
        except KeyboardInterrupt:
            # /nucleate drives the whole FSM inline on this thread. Without this
            # the Ctrl+C escapes main() and kills the REPL with a raw traceback —
            # the __main__ guard at the bottom never runs, since the installed
            # entry point is the `silica = silica.cli:main` console script.
            # Not "use /revert": an interrupted run that committed nothing has no
            # journalled inverses, so last_active_run() would walk back to an
            # EARLIER run and undo that one instead.
            CONSOLE.print("\n  [dim](interrupted — chunks that already committed stay in the vault)[/]")
            continue
        if expanded is not None:
            if not expanded:
                continue  # shortcut fully handled inline (e.g. /nucleate of code files)
            user_input = expanded
            is_directive = True

        # /web — the consent turn: a normal agent turn with web-only tools and
        # citations built from the tool trace. Checked before the slash handler,
        # since it rewrites the input into an ordinary agent instruction.
        web: tuple[str, str] | None = None
        try:
            web = _expand_web_turn(user_input, messages)
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
                    capture_session(messages, session_id=session_id, driver="tui",
                                    event="session_clear")
                CONSOLE.clear()
                print_home()
                messages[:] = _fresh_messages()
                collapsed = set()  # indices reset with the history
                seen_numbers = set()
                from silica.obs import narration as _narr
                _narr.NARRATOR.close()          # next user turn opens a fresh sid
                session_id = uuid.uuid4().hex[:12]
                continue

            result = _handle_slash_command(user_input, messages)
            if cmd.startswith("/resume"):
                # From the narration the history came back from. A /resume that
                # failed left the narrator on this session, whose narration holds
                # what the live set does.
                from silica.obs import narration as _narr

                seen_numbers = numbers_of_session(_narr.NARRATOR.sid)
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

        # The notes the user named, as paths (ADR-0097 point 2), the same block
        # the GUI's run_turn adds. Not on a /web turn: its tools cannot read a note.
        if web is None:
            from silica.kernel.text.mentions import named_notes

            if block := named_notes(typed):
                user_input += "\n\n" + block

        # Normal user message → agentic loop. CLI-expanded shortcuts carry an
        # `origin` so the wire boundary (and our own bookkeeping) can tell a
        # harness directive apart from a human turn.
        msg: dict = {"role": "user", "content": user_input}
        if is_directive:
            msg["origin"] = "cli"
        from silica.kernel import personas

        if persona := personas.active(messages):
            msg[personas.TURN_KEY] = persona
        messages.append(msg)

        # Born at the first user turn (spec §5); idempotent afterwards. The
        # sid is capture's session_id so the two records share one id space.
        from silica.obs import narration as _narr
        _narr.NARRATOR.ensure_session(driver="tui", sid=session_id)
        session_id = _narr.NARRATOR.sid or session_id   # /resume may have switched it
        _narr.NARRATOR.turn(msg)

        # Both wrappers forward every event to the renderer untouched: WebTurn
        # records the trace the citations are built from, RecallWatch counts
        # recall misses for the thin-coverage hint.
        # A /web turn is not checked for numbers, but its pages were read: the
        # RecallWatch under its WebTurn collects them for the session set.
        interrupt = _TurnInterrupt(callback)
        read = RecallWatch(interrupt, surface="repl")
        watch = WebTurn(web[0], read) if web else read

        # Only around the turn: at the prompt, prompt_toolkit owns Ctrl+C.
        previous_sigint = signal.signal(signal.SIGINT, interrupt.on_sigint)
        try:
            answer = run_agent(
                messages,
                model=CONFIG.model,
                tool_progress_callback=watch,
                cancel_token=interrupt.token,
                constraints=(
                    # interactive=True: the chat_tools cut must not also demote
                    # the REPL to a worker (no streaming, worker-slot capped) —
                    # which is what it silently did when the toolset constraint
                    # was first wired in.
                    web_turn_constraints() if web
                    else AgentConstraints(tools=chat_tools(messages),
                                          deferred=chat_deferred(messages), interactive=True)
                ),
            )
            if interrupt.token.is_set():
                # A drained tool hands back the loop's "(silica: cancelled)",
                # which is no answer: the turn ends the way a raise ends it.
                raise KeyboardInterrupt
            # ADR-0095: the answer as the model wrote it, before attribution adds
            # text it did not; not a /web turn's, whose answer is bound to banked
            # verbatim quotes.
            unread = [] if web else unfounded_in_turn(answer, read.numbers, messages, seen_numbers)
            if web:
                answer = watch.attribute(answer, messages)
            elif watch.web_answer:
                from silica.sources.web_research import relay_sources

                answer = relay_sources(answer, messages)
            # The lines under the answer, stored on it before the beat below
            # so the session keeps them (ADR-0094 point 8).
            from silica.kernel.learn.artifacts import grounding_notes

            notes = turn_notes.attach(messages, ([] if web else watch.notes()) + numbers_notes(unread)
                                      + ([] if web else grounding_notes(typed, answer)))
            # Final-assistant turn beat, post-attribution (see loop.py): the
            # replay must carry what the user actually read, sources included.
            if messages and messages[-1].get("role") == "assistant":
                _narr.NARRATOR.turn(messages[-1])
            if answer:
                CONSOLE.print()
                CONSOLE.print("[role.assistant]⏺ silica[/]")
                CONSOLE.print(FlatMarkdown(answer))
                CONSOLE.print()
            _print_turn_notes(notes)
            # run_agent already appended the final assistant message to the
            # history — re-appending `answer` here would store it twice.
            _update_context_tokens(messages)
            collapsed = _compact_context(messages, collapsed)
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
        seen_numbers |= read.numbers

    # Every exit from the REPL passes here: /exit, Ctrl+D, Ctrl+C at the prompt.
    if not user_memory.incognito():
        capture_session(messages, session_id=session_id, driver="tui")
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
