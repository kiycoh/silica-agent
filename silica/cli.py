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
import re
import shlex
from pathlib import Path
import sys
import uuid
from collections.abc import Callable
from contextlib import nullcontext, redirect_stdout
from typing import NamedTuple

from silica.ui.style import FlatMarkdown

from silica.agent.constraints import AgentConstraints, chat_deferred, chat_tools, web_turn_constraints
from silica.agent.loop import run_agent
from silica.agent.recall_watch import THIN_COVERAGE_HINT, RecallWatch
from silica.config import CONFIG
from silica.onboarding.adopt import resolve_cwd_vault
from silica.prompts import _lang_prefer, system_prompt
from silica.ui.console import CONSOLE
from silica.ui.home import print_home
from silica.ui.prompt import build_session, bottom_toolbar, prompt_text
from prompt_toolkit.patch_stdout import patch_stdout

# Import tools to trigger registration via @tool decorator
import silica.tools.atomic  # noqa: F401
import silica.tools.composed  # noqa: F401
import silica.tools.wrapped  # noqa: F401
import silica.tools.codedocs_tool  # noqa: F401
import silica.tools.delegate_tool  # noqa: F401
import silica.tools.loader  # noqa: F401
import silica.tools.tabular  # noqa: F401
import silica.sources.web_research  # noqa: F401  (registers the web_search, web_fetch, remember and plan tools)
from silica.sources.web_research import WebTurn

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
    # in the meter as context the provider is charged for, when `_to_wire` drops
    # it before the request.
    from silica.agent.llm import _text_of
    from silica.agent.providers import _to_wire

    messages = [_to_wire(m) for m in messages]
    try:
        enc = _cl100k()

        def n(text: str) -> int:
            # disallowed_special=(): a note that quotes "<|endoftext|>" is text to
            # count, and tiktoken's default raises on it, which sent the whole
            # window to the fallback below.
            return len(enc.encode(text, disallowed_special=()))

        total = sum(
            n(_text_of(m.get("content"))
              + (json.dumps(m["tool_calls"]) if m.get("tool_calls") else ""))
            + _TOKENS_PER_MESSAGE
            for m in messages
        )
        return total + (n(json.dumps(tools)) if tools else 0)
    except Exception:
        # No encoding (a broken tiktoken install): a rough meter beats none.
        return sum(len(_text_of(m.get("content"))) for m in messages) // 4


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
    from silica.agent.constraints import chat_tools
    from silica.tools import TOOLS

    return [TOOLS[n].json_schema() for n in chat_tools(messages) if n in TOOLS]


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
        from silica.kernel.recall.vault_map import build_vault_map

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
    from silica.kernel.vault_manifest import active_write_dir
    from silica.onboarding.adopt import SAFE_WRITE_DIR

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


def seed_messages(math: bool = False) -> list[dict]:
    """The system context a fresh conversation starts from: prompt, date, vault
    scope, vault map, and the restated language rule.

    Shared by the TUI and the GUI (`ui/web/server._build_seed`, which passes
    math=True for the MathML renderer). It used to be TUI-only, and the GUI's
    own seed had drifted to prompt + map: no `_vault_scope`, so the model was
    blind to `write_dir`, and no closing language line, which is the fix commit
    93b6d2d shipped here and never carried across. One builder, no drift.
    """
    from silica.onboarding.checks import reply_language_for

    # Explicit conventions, else the vault's own language (declared, else
    # detected from the human notes): a slash-command turn carries no language
    # of its own, and defaulting to English on an Italian vault answered /quiz
    # in the wrong language.
    reply = reply_language_for(CONFIG.vault_path) or ""
    messages: list[dict] = [{"role": "system", "content": system_prompt(reply, math=math)}]
    messages.append({"role": "system", "content": _today_line()})
    messages.append({"role": "system", "content": _vault_scope()})
    _inject_vault_map(messages)
    # User memory (ADR-0076), frozen for the session: a save lands on disk and
    # in its tool result and shows here from the next session, so the model's
    # picture of the user does not move under it mid-conversation. After the
    # map and before the language line, which must stay last.
    from silica.kernel import user_memory

    if (memory := user_memory.snapshot_message()) is not None:
        messages.append(memory)
    # The vault map is the vault's own language. On a vault whose notes are not
    # in `reply`, that bulk drowns the language rule sitting in message 0, and
    # the model answers in the notes' language. Restate it last, closest to the
    # user turn.
    messages.append({"role": "system", "content": _lang_prefer(reply)})
    return messages


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
    if debug:
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

    ponytail: one file shared by every silica process (REPL, GUI, MCP) in
    append mode; a rotation by one process leaves the others writing to the
    renamed file until they rotate themselves. Per-pid files if that ever
    loses a line that mattered.
    """
    from logging.handlers import RotatingFileHandler
    from silica.kernel.recall.paths import _SILICA_HOME

    root = logging.getLogger()
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return
    # Handlers without a level of their own showed what root allowed (the MCP
    # wiring is a bare basicConfig): pin that before root opens up, or the
    # terminal starts printing DEBUG.
    shown = root.level or logging.WARNING
    for h in root.handlers:
        if h.level == logging.NOTSET:
            h.setLevel(shown)
    path = _SILICA_HOME / "logs" / "silica.log"
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
    from silica.kernel.recall.paths import repo_root_warning
    from silica.kernel.recall.relatedness import reset_vault_caches
    from silica.kernel.text.overlay import reset_overlay_cache
    from silica.kernel.vault_manifest import (
        apply_manifest_to_config,
        get_active_manifest,
        reset_manifest_cache,
    )
    from silica.onboarding.adopt import declare_write_dir, seed_silicaignore
    from silica.onboarding.checks import language_status

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

    Do NOT pin an MCP server this way: a stdio client (Claude Code) spawns the
    server with cwd set to the project it opened, so cwd is already the answer,
    and a pin in the server's env silently serves one vault to every project.
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
    from silica.kernel.recall.paths import repo_root_warning

    warn = repo_root_warning(CONFIG.vault_path)
    if warn:
        CONSOLE.print(f"  [yellow]⚠ {warn}[/]")


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
        from silica.onboarding.checks import language_status

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
        from silica.agent import narration as _narr
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
    from silica.tools import TOOLS

    k = _int_flag(args, "--k=", 5)
    min_trust = _str_flag(args, "--min-trust").strip().lower()
    life = _str_flag(args, "--lifecycle").strip().lower()
    # original case preserved — raw_input, not the lowered cmd
    query = " ".join(_positional(args))
    if not query:
        CONSOLE.print("  Usage: /find <query> [--k=N] [--min-trust=human|grounded|distilled|kept] "
                      "[--lifecycle=active|review|contested|superseded]")
        return True
    result = TOOLS["silica_semantic_search"].run(query=query, k=k)
    try:
        parsed = json.loads(result)
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
    from silica.kernel.write import frontmatter
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


def _dc_verify(args: list[str], **_) -> bool:
    """/verify <note> [--clear] — a person vouches for a note (OKF §5.2), bound
    to the body as it is now; --clear withdraws every human entry. The only
    writer of a `human:` attestation: the MCP tools refuse the key (spec M4)."""
    import datetime
    import getpass

    from silica.driver import DRIVER
    from silica.kernel.write.checkpoints import get_checkpoint_store
    from silica.kernel.write.notetype import clear_verified, stamp_verified

    name = " ".join(_positional(args))
    if not name:
        CONSOLE.print("  Usage: /verify <note> [--clear]")
        return True
    try:
        nc = DRIVER.read_note(name)
    except Exception as e:
        CONSOLE.print(f"  [yellow]Cannot read '{name}': {e}[/]")
        return True
    path, prior = nc.ref.path or name, nc.content or ""
    who, today = f"human:{getpass.getuser()}", datetime.date.today().isoformat()
    try:
        new = clear_verified(prior) if "--clear" in args else stamp_verified(prior, who, today)
    except ValueError as e:
        CONSOLE.print(f"  [yellow]{e}[/]")
        return True
    if new == prior:
        CONSOLE.print(f"  {path}: nothing to change.")
        return True
    DRIVER.overwrite(path, new)
    get_checkpoint_store().push(path, prior, new)  # /undo reaches it; the edited lane knows it
    if "--clear" in args:
        CONSOLE.print(f"  {path}: human attestations cleared.")
    else:
        CONSOLE.print(f"  {path}: verified by {who} at {today}.")
    return True


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
    from silica.kernel.recall.mindmap import note_resolver, reading_path
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
    path = reading_path(src, dst, weighted=True)
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
    from silica.kernel.recall.paths import vault_epoch
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
    """/undo [note-path] — revert the last write to one note, or bring back a
    note the chat deleted."""
    from silica.driver import DRIVER
    from silica.kernel.write.checkpoints import get_checkpoint_store
    from silica.kernel.write.undo_journal import get_undo_journal, revert_run

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
    rest = raw_input.strip().split(maxsplit=1)
    deleted = None
    note_path: str | None
    if len(rest) > 1:
        note_path = rest[1].strip()
        if not _on_disk(note_path):
            deleted = journal.last_chat_delete(vault, note_path)
    else:
        # The newest action wins, a write or a delete: a delete lives in the
        # undo journal, not in the checkpoint store the writes use.
        last_write = store.most_recent()
        last_delete = journal.last_chat_delete(vault)
        if last_delete and not _on_disk(last_delete[1]) and (
                last_write is None or last_delete[2] >= last_write[1]):
            deleted = last_delete
        note_path = deleted[1] if deleted else (last_write[0] if last_write else None)
    if deleted:
        res = revert_run(deleted[0])
        if res["reverted"]:
            CONSOLE.print(f"  Restored: [bold]{deleted[1]}[/] (deleted from the chat)")
        else:
            why = (res["skipped"] or res["errors"] or [{"reason": "unknown"}])[0]
            CONSOLE.print(f"  [yellow]Not restored:[/] {deleted[1]} "
                          f"({why.get('reason') or why.get('error')})")
        return True
    if not note_path:
        CONSOLE.print("  Nothing to undo — no patches recorded in this session.")
        return True

    content = store.undo(note_path)
    if content is None:
        CONSOLE.print(f"  [yellow]Nothing to undo for[/] {note_path} (already at original).")
        return True

    try:
        DRIVER.overwrite(note_path, content)
        depth = store.depth(note_path)
        remaining = max(0, depth - 1)
        CONSOLE.print(f"  Undone: [bold]{note_path}[/]  [dim]({remaining} undo step(s) remaining)[/]")
    except Exception as exc:
        CONSOLE.print(f"  [red]Undo failed:[/] {exc}")
    return True


def _revert_by_source(source: str, vault: str | None) -> bool:
    """Every journalled run the ledger attributes to `source`, newest first,
    scoped to that source's notes; what the journal does not hold is named,
    never guessed at (legacy runs predate the join and stay /revert <run-id>)."""
    from silica.kernel.write.undo_journal import revert_source

    res = revert_source(source, vault=vault)
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
        from silica.kernel.recall.run_log import append_log_line, format_revert_event
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
    from silica.kernel.write.undo_journal import get_undo_journal, revert_run
    vault = CONFIG.vault_path.strip() or None
    tokens = list(args) or raw_input.strip().split()[1:]
    source = next((t.split("=", 1)[1] for t in tokens if t.startswith("--source=")), "")
    if "--source" in tokens and tokens.index("--source") + 1 < len(tokens):
        source = tokens[tokens.index("--source") + 1]
    if source:
        return _revert_by_source(source, vault)
    parts_split = raw_input.strip().split(maxsplit=1)
    run_id = parts_split[1].strip() if len(parts_split) > 1 else _latest_revertable(vault)
    if not run_id:
        CONSOLE.print("  Nothing to revert — no runs recorded for this vault.")
        return True
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
    res = revert_run(run_id)
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
        from silica.kernel.recall.run_log import append_log_line, format_revert_event
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


def _dc_connect(args: list[str], *, raw_input: str = "", **_) -> bool:
    """/connect [list|add <json>|secure|login|logout|remove <key>|reload]: MCP servers (ADR-0091)."""
    from silica.connectors import connect_cli

    # The raw line: `add` takes a JSON object whose spaces whitespace
    # splitting would scatter.
    connect_cli.run(raw_input or " ".join(["/connect", *args]), CONSOLE)
    return True


def _dc_review(args: list[str], **_) -> bool:
    """/review [--flush=<hash>] — the deferred store: write bundles parked
    by a gate, listed or flushed. Not the study queue: that is /learn."""
    from silica.kernel.recall.deferred import get_deferred_store
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
    model in between, so neither write gate applies (ADR-0076 §6): it is the
    way to save from a conversation the taint gate closed. A bare /remember
    is not handled here; it falls through to _SHORTCUTS and asks the model."""
    from rich.markup import escape

    from silica.kernel import user_memory

    text = remember_text(raw_input)
    if not text:
        return False
    out = user_memory.save("vault", text)
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
        from silica.agent import narration as _narr
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
    "/agenda": _dc_agenda,
    "/episodes": _dc_episodes,
    "/changes": _dc_changes,
    "/undo": _dc_undo,
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


def _handle_direct_shortcut(raw_input: str, messages: list[dict]) -> bool:
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
    """

    parts = raw_input.strip().split()
    if not parts:
        return False
    cmd = parts[0].lower()

    handler = _DIRECT.get(cmd)
    if handler is None:
        return False
    # `messages` for /incognito, which hides the memory message of the
    # conversation in progress; every other handler ignores it.
    return handler(parts[1:], raw_input=raw_input, messages=messages)


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
    from silica.agent.events import BatchRunStartEvent

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
    from silica.agent import narration as _narr_mod
    # Left running on purpose: a seeded ledger IS pending until executed, and
    # a projection showing it running is the truth, not a leak.
    _narr_mod.NARRATOR.span_open(
        "run", f"run-{run.run_id}", f"{kind} {label}: {len(payloads)} batch(es)",
        {"run_id": run.run_id, "kind": kind, "total": len(payloads)})
    return (
        f"A ledger for /{kind} has been created with {len(payloads)} chunk(s) "
        f"(run_id={run.run_id}). Use `silica_ledger_next` with this run_id to execute them."
    )


def _announced_target(target_dir: str) -> str:
    """The folder the user will find the notes in, write-boundary included.

    Validate rebases every write into the vault's `write_dir`, so announcing
    the raw target ("nucleate: 2 file(s) → appunti") named a folder nothing
    lands in — the user opened it, found nothing, and concluded the run wrote
    nothing. Display-only: the Coordinator still takes the raw target.
    """
    try:
        from silica.kernel.vault_manifest import in_write_dir

        return in_write_dir(target_dir)
    except Exception:
        return target_dir


def _draft_title(body: str, stem: str) -> str:
    """First `# ` heading, else the file stem. Never a prose fragment: the
    audit's `minutes of compute` came from letting a model pick a mid-sentence
    span, so this stays mechanical."""
    for line in body.splitlines():
        if line.startswith("# ") and line[2:].strip():
            return line[2:].strip().replace("/", "-")
    return stem


def _file_drafts(
    md_files: list[str], target_dir: str, undo_run: str | None
) -> list[str]:
    """File draft-form sources instead of distilling them; return the rest.

    docs/specs/nucleation-forms.md: a draft is the owner's in-progress
    artifact — both distillation outcomes observed on drafts were defects
    (silent drop, verbatim paste under a fragment title). Filing = one note at
    the resolved target, body intact, `form: draft` frontmatter, /revert
    coverage, source archived. Interactive runs confirm; headless runs file
    and report. A "n" answer sends the file down normal nucleation under the
    vault fallback profile.
    """
    from pathlib import Path

    import silica.kernel.forms as forms
    from silica.driver import DRIVER
    from silica.kernel.write import frontmatter
    from silica.sources.registry import _record_inverse
    from silica.tools.wrapped import silica_cleanup

    kept: list[str] = []

    # Resolve every file's form upfront on a small pool: an unstamped file
    # costs one sniff LLM call and the calls are independent — 14 lecture
    # files paid ~15s serially for verdicts this loop then consumes in order.
    # The interactive draft prompt below stays sequential.
    def _read_and_resolve(f: str):
        try:
            text = forms.read_source_text(f)
            return text, forms.resolve(text)
        except Exception:
            return None

    if len(md_files) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(md_files))) as pool:
            resolved = list(pool.map(_read_and_resolve, md_files))
    else:
        resolved = [_read_and_resolve(f) for f in md_files]

    # Siblings of one folder are one kind of material, and the sniff is not
    # stable on them: the same 14 lectures came back 6 study / 4 transcript /
    # 4 unsure in one run and one of them "draft" in the next, which filed a
    # lecture as-is instead of distilling it (2026-09-05). The folder votes
    # among the sniffed verdicts; sniffed and unsure files follow the vote,
    # stamps and a configured vault profile do not. A tie keeps each verdict.
    if len(md_files) > 1:
        from collections import Counter
        votes = Counter(rr[1].form for rr in resolved if rr and rr[1].origin == "sniff" and rr[1].form)
        top = votes.most_common(2)
        if top and (len(top) == 1 or top[0][1] > top[1][1]):
            folder_form = top[0][0]
            voted = forms.Form(folder_form, forms.profile_for(folder_form), "folder vote")
            for i, rr in enumerate(resolved):
                if rr and rr[1].origin in ("sniff", "default") and rr[1] != voted:
                    forms.pin_sniff(rr[0], folder_form)
                    resolved[i] = (rr[0], voted)
            CONSOLE.print(
                f"  folder vote: [bold]{folder_form}[/] "
                f"({votes[folder_form]} of {sum(votes.values())} sniffed file(s))"
            )

    for f, rr in zip(md_files, resolved):
        if rr is None:
            kept.append(f)
            continue
        text, res = rr
        if res.form != "draft":
            # Visibility is non-negotiable (nucleation-forms spec): the lens
            # verdict prints where auto-target is announced, never silently.
            CONSOLE.print(
                f"  {f}: profile [bold]{res.profile or 'default'}[/] ({res.origin})"
            )
            kept.append(f)
            continue
        if sys.stdin.isatty():
            ans = CONSOLE.input(
                f"  {f} looks like a draft of yours ({res.origin}) — file it "
                f"as-is instead of distilling? [Y/n] "
            ).strip().lower()
            if ans in ("n", "no"):
                kept.append(f)
                continue
        try:
            dest_dir = target_dir or _pick_target_folder([f])
        except Exception:
            CONSOLE.print(f"  [yellow]{f}: draft, but no target resolved — distilling instead[/]")
            kept.append(f)
            continue
        # The filed copy is a Silica write like any other: composed into the
        # write boundary. A bare-folder pick used to land the draft in the
        # user's source tree, beside their own files.
        from silica.kernel.vault_manifest import in_write_dir
        dest_dir = in_write_dir(dest_dir)
        data, _, body = frontmatter.split(text)
        data = dict(data or {})
        data["form"] = "draft"
        data.setdefault("source_file", Path(f).name)
        fm_lines = "".join(f"{k}: {json.dumps(v) if isinstance(v, str) else v}\n"
                           for k, v in data.items())
        note = f"---\n{fm_lines}---\n\n{body.strip()}\n"
        title = _draft_title(body, Path(f).stem)
        note_rel = f"{dest_dir}/{title}.md"
        prior: str | None = None
        try:
            prior = DRIVER.read_note(note_rel).content
        except Exception:
            prior = None
        if prior is not None:
            note_rel = f"{dest_dir}/{title} ({Path(f).stem}).md"
            prior = None
        DRIVER.upsert(note_rel, note)
        if undo_run:
            _record_inverse(undo_run, note_rel, prior)
        silica_cleanup(f)
        CONSOLE.print(
            f"  filed draft ({res.origin}): [bold]{note_rel}[/] — body kept intact"
        )
    return kept


# Silica's own bookkeeping folders — never a destination for a concept note.
# Casefolded at the comparison: the archive is `Done` since 2026-08-23 while a
# vault that predates the rename keeps its `done`, and one set must answer for
# both spellings or the older vault starts offering its archive as a target.
_CENSUS_SKIP = {"done", "sources", "images", "logs", "attachments"}


def _prior_conversions() -> dict[str, dict]:
    """abspath of a converted source → {"inbox": [segment paths], "done": n}.

    The two places a source's converted segments can already live: still in the
    inbox (a run that never finished) and archived in done/ (a run that did).
    Read once per /nucleate invocation off the `source_file` frontmatter that
    convert() stamps on every segment — the identity `_resolve_input` produced
    at conversion time, so the same resolver matches it on re-run.
    """
    from pathlib import Path

    from silica.driver import DRIVER
    from silica.kernel.vault_manifest import active_done_dir, active_inbox_dir
    from silica.kernel.write import frontmatter

    out: dict[str, dict] = {}
    vault = Path(CONFIG.vault_path or "")

    def _scan(rels: list[str], bucket: str) -> None:
        for rel in rels:
            try:
                data, _, _ = frontmatter.split(
                    (vault / rel).read_text(encoding="utf-8"))
            except Exception:
                continue
            src = str((data or {}).get("source_file") or "")
            if not src:
                continue
            entry = out.setdefault(src, {"inbox": [], "done": 0})
            if bucket == "inbox":
                entry["inbox"].append(rel)
            else:
                entry["done"] += 1

    if active_inbox_dir():
        _scan([r.path for r in DRIVER.list_inbox_files()
               if r.path.endswith(".md")], "inbox")
    done = active_done_dir()
    if done:
        _scan([r.path for r in DRIVER.list_files(done)], "done")
    from silica.tools.atomic import _natural_key
    for entry in out.values():
        entry["inbox"].sort(key=_natural_key)  # segment order is ingest order
    return out


def _shallow_folders(vault_path: str, max_depth: int = 2) -> set[str]:
    """Vault-relative folders holding at least one file, down to `max_depth`.

    ponytail: depth-capped instead of a full walk — a scanned-book library has
    one folder per photographed artefact and the deep tail is noise, not a
    destination. Raise the cap if someone files notes deeper than two levels
    in a folder that holds no markdown yet.
    """
    from pathlib import Path

    root = Path(vault_path or "")
    if not root.is_dir():
        return set()
    out: set[str] = set()
    for depth in range(1, max_depth + 1):
        for d in root.glob("/".join(["*"] * depth)):
            if not d.is_dir():
                continue
            rel = d.relative_to(root).as_posix()
            if any(p.startswith(".") for p in d.relative_to(root).parts):
                continue
            if any(c.is_file() and not c.name.startswith(".") for c in d.iterdir()):
                out.add(rel)
    return out


def _pick_target_folder(md_files: list[str]) -> str:
    """Choose the destination folder for a nucleate run with ONE small LLM call.

    Replaces a full agent turn: the old auto-target path resent the entire
    session history to the orchestrator to make this single decision.
    Raises on any failure — the caller falls back to the agent message.
    """
    from pathlib import Path

    from silica.agent.llm import call_llm
    from silica.driver import DRIVER

    from silica.kernel.vault_manifest import active_inbox_dir

    inbox = active_inbox_dir() or "Inbox"
    # Note parents (any depth) PLUS the shallow tree of folders that hold
    # anything at all. A research library is folders of PDFs: censusing note
    # parents alone showed the model an EMPTY list, whereupon it echoed back the
    # inbox subfolder the source sat in and VALIDATE rejected all 31 ops.
    folders = {
        str(Path(r.path or r.name).parent)
        for r in DRIVER.list_files("")
    } | _shallow_folders(CONFIG.vault_path)
    listed = sorted(
        f for f in folders - {".", inbox}
        if not f.startswith(f"{inbox}/")
        and not any(part.startswith(".") or part.casefold() in _CENSUS_SKIP
                    for part in Path(f).parts)
    )
    # read_source_text, not DRIVER.read_note: a .txt inbox source (draft
    # filing's ep180 case) must be auto-targetable too.
    from silica.kernel.forms import read_source_text

    excerpt = read_source_text(md_files[0])[:1500]
    prompt = (
        "Pick the single most relevant destination folder for nucleating this "
        "content into a knowledge vault. Reply with ONLY the folder path on one "
        "line, no quotes. Prefer an existing folder; invent a sensible new path "
        "only if nothing fits.\n\n"
        "Existing folders:\n" + (
            "\n".join(f"- {f}" for f in listed[:200]) if listed
            # Say it, never leave the list blank: on a fresh vault the model
            # spent its whole reply on "nothing follows 'Existing folders:'"
            # before guessing (473 completion tokens for two words, 2026-08-23).
            else "(no folders yet: this vault holds only its inbox, so name a "
                 "sensible new top-level folder)"
        ) + f"\n\nContent excerpt ({md_files[0]}):\n{excerpt}"
    )
    resp = call_llm(CONFIG.model, [{"role": "user", "content": prompt}], max_tokens=2048)
    lines = [ln.strip().strip('"').strip("`").rstrip("/") for ln in (resp.text or "").splitlines()]
    pick = next((ln for ln in lines if ln), "")
    if not pick:
        raise ValueError("empty folder pick")
    # Same rule VALIDATE applies to every op, asked once here instead of once
    # per rejected op an hour of LLM calls later.
    from silica.kernel.recall.paths import contain_in_vault, is_inbox_path

    # The excerpt above is INGESTED DOCUMENT text, so this reply is an
    # injection channel and the string it carries becomes a write destination
    # (`<dest>/<title>.md` → DRIVER.upsert). Contain it here, where the caller
    # still has a fallback, rather than letting an absolute or `..`-bearing
    # pick reach the driver: `in_write_dir` only prefixes, it never normalises.
    norm = contain_in_vault(pick.replace("\\", "/"), Path(CONFIG.vault_path))
    if is_inbox_path(f"{norm}/x.md"):
        raise ValueError(f"folder pick {pick!r} lands in the inbox")
    return norm


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


def _stage_envelope(body: str, stem: str, inbox: str) -> str:
    """Put one rendered conversation in the vault inbox for the FSM.

    The WAL lives outside the vault on purpose, but the FSM reads its sources
    through the driver, vault-relative (`to_vault_relative`), so the drain
    stages the rendered prose the way /web stages its findings: zero-trust
    ingress lands in the inbox, the gate decides what survives. The staged file
    is discarded by `_discard_staged` once the run is over, so the conversation
    never becomes a vault resident. Returns the vault-relative staged path.
    """
    from silica.driver import DRIVER

    rel = f"{inbox}/{stem}.md"
    DRIVER.upsert(rel, body)
    return rel


def _episodic_distill(content: str, envelope: dict, *, run_id: str,
                      target: str) -> bool:
    """Harvest facts from one of Silica's own sessions. Writes no note.

    Machine memory enters the vault only by explicit promotion, so this branch
    keeps the distiller and throws its note body away: `ephemerals` is the whole
    harvest, and it lands in the episodic store like any other run's. Nothing is
    staged either — the transcript never becomes a vault file, not even one that
    gets deleted afterwards.
    ponytail: linear, no FSM — no chunk steering, no per-chunk retry, no write
    gate, because nothing is written. A failure leaves the envelope pending and
    the next drain repeats the call.
    """
    from silica.kernel import prep_delegation
    from silica.kernel.partition import partition_by_concepts
    from silica.kernel.recall.episodic import (
        EpisodicStore,
        capture_from_distill,
        key_vocabulary_section,
    )
    from silica.kernel.recall.paths import vault_digest
    from silica.kernel.text.keyphrase import extract_keyphrases
    from silica.kernel.text.payload import build_concept_entry

    concepts = [c.phrase for c in
                extract_keyphrases(content, lang=CONFIG.cooccurrence_lang)]
    if not concepts:
        return True  # a conversation with nothing to name has nothing to remember
    # Assembled here rather than through build_payload, which reads its source
    # back through the driver — i.e. would require this conversation to be a
    # vault file first. No collision search either: the vault is not the
    # destination, so the hits would only plan note edits this branch discards.
    payload = {"schema_version": 1, "batches": [{
        "inbox_file": f"{run_id}.md",
        "concepts": [
            build_concept_entry(name=c, inbox_content=content, collision=None,
                                in_new_concepts=True, window=450)
            for c in sorted(concepts)
        ],
    }]}
    seen = (envelope.get("captured_at") or "")[:10]
    ok = True
    for chunk in partition_by_concepts(payload, 7) or [payload]:
        # ADR-0021: the established keys, so the distiller reuses them instead
        # of coining a synonym per session — a chain that never lands twice on
        # the same key never reaches min_days, and the promotion queue stays
        # empty by construction. Re-read per chunk: the previous chunk's facts
        # are already in the store. Only this section, never build_substrate:
        # the vault's related notes have no business in a machine-memory prompt.
        result = prep_delegation.run_distiller(
            payload=chunk, target=target, session_date=seen,
            substrate=key_vocabulary_section(EpisodicStore()),
            # This lane keeps only ephemerals — never generate note bodies.
            structure_only=True,
        )
        if result.get("error"):
            logger.warning("drain: episodic distill failed (%s)", result["error"])
            ok = False
            continue
        # The run id is the envelope name: one session is one run, which is
        # exactly the unit `nucleation_candidates` counts. Note attribution is
        # session-level on purpose — every fact of an envelope carries the same
        # list. ponytail: per-fact attribution if the graph overlay gets noisy.
        capture_from_distill(result, run_id=run_id, seen=seen,
                             vault=vault_digest(CONFIG.vault_path),
                             notes=list(envelope.get("notes_touched") or []))
    return ok


def _discard_staged(rel: str) -> None:
    """Remove a staged transcript from the vault, wherever the run left it.

    A successful FSM run archives the source into `done/`, so both paths are
    tried: the point is that no conversation text stays in the vault after the
    drain, archived or not.
    """
    from pathlib import Path as _Path

    from silica.driver import DRIVER

    for candidate in (rel, f"done/{_Path(rel).name}"):
        try:
            DRIVER.delete(candidate)
        except Exception:
            continue


# Terminal FSM verdicts that leave nothing to retry: notes written, nothing
# worth writing, or this source already committed by an earlier run. Anything
# else — "partial", "failed", or no verdict at all — keeps the envelope pending.
# The FSM's verdict is the criterion, not `context["error"]`: best-effort phases
# record an error and carry on to Success (orchestrator._on_step_error).
_DRAIN_SETTLED = {"Success", "no_ops", "already_nucleated"}


def _drain_wal() -> str:
    """`/nucleate` with no argument: drain this vault's capture WAL.

    A batch at a time (`collect`'s cap), so a 500-conversation import backlog
    becomes deliberate, resumable runs instead of one LLM bill.
    """
    import silica.capture as capture

    vault = CONFIG.vault_path.strip()
    if not vault:
        return "No vault is configured, so there is nothing to drain. Say so in one line."

    capture.housekeep(vault)
    envelopes, remaining = capture.collect(vault)
    if not envelopes:
        CONSOLE.print("  nothing captured to drain.")
        return ""

    from silica.kernel.vault_manifest import active_inbox_dir
    from silica.sources.transcript import render
    inbox = active_inbox_dir() or "Inbox"
    staged: dict = {}
    episodic = 0
    for env_path in envelopes:
        try:
            envelope = json.loads(env_path.read_text(encoding="utf-8"))
            body = render(envelope)
            # Own sessions never take the note path (spec §11), so they are
            # never staged: no vault write, nothing to undo afterwards.
            rel = ("" if not body or envelope.get("source") == "silica"
                   else _stage_envelope(body, env_path.stem, inbox))
        except Exception as exc:
            logger.warning("drain: unreadable envelope %s (%s)", env_path.name, exc)
            capture.mark_failed(env_path)
            continue
        if not body:
            capture.mark_processed(env_path)  # nothing said, nothing to keep
            continue
        if rel:
            staged[env_path] = rel
            continue
        episodic += 1
        if _episodic_distill(body, envelope, run_id=env_path.stem, target=inbox):
            capture.mark_processed(env_path)
        else:
            remaining += 1  # pending: the next drain repeats the call
    if episodic:
        CONSOLE.print(f"  drain: {episodic} session(s) → episodic memory")
    if not staged:
        if not episodic:
            CONSOLE.print("  nothing captured to drain.")
        elif remaining:
            CONSOLE.print(f"  {remaining} envelope(s) still pending, run /nucleate again")
        return ""

    files = list(staged.values())
    try:
        target_dir = _pick_target_folder(files)
    except Exception as exc:
        logger.debug("drain: auto-target pick failed (non-fatal): %s", exc)
        target_dir = "Sessions"
    CONSOLE.print(f"  drain: {len(files)} conversation(s) → [bold]{target_dir}[/]")

    from silica.router.coordinator import Coordinator
    try:
        result = Coordinator(inbox_files=files, target_dir=target_dir).run()
    finally:
        # Unstaging is not part of the happy path: a crash or a Ctrl+C must not
        # leave a raw conversation sitting in a committable vault.
        for rel in staged.values():
            _discard_staged(rel)
    # ponytail: batch-level outcome — one failed chunk leaves the whole batch
    # pending, and the next run re-drains it (the FSM's own dedup absorbs the
    # repeat). Per-envelope status if a mixed batch ever costs a real re-run.
    ok = result.get("final_status") in _DRAIN_SETTLED
    for env_path in staged:
        if ok:
            capture.mark_processed(env_path)

    status = result.get("final_status") or result.get("error") or "done"
    left = remaining + (0 if ok else len(staged))
    tail = f" — {left} envelope(s) still pending, run /nucleate again" if left else ""
    CONSOLE.print(f"  drain finished: [bold]{status}[/]{tail}")
    return ""


def _promote(args: list[str]) -> str:
    """`/promote [<key>]` — the consent bridge out of episodic memory.

    Bare: list what the store thinks is worth keeping. With a key: render that
    chain into a stub and send it through the ordinary nucleate gate, which is
    the point — machine memory earns a note the same way any other source does.
    """
    from silica.kernel.recall.episodic import EpisodicStore, entity_key

    store = EpisodicStore()
    keys = _positional(args)
    if not keys:
        candidates = store.nucleation_candidates()
        if not candidates:
            CONSOLE.print(
                "  No episodic candidates yet — nothing has come up in enough "
                "sessions to be worth a note."
            )
            return ""
        groups: dict[str, list] = {}
        for c in candidates:
            groups.setdefault(entity_key(c.key), []).append(c)
        CONSOLE.print(f"  {len(groups)} episodic candidate(s):")
        for ent, members in sorted(groups.items()):
            attrs = " · ".join(f"{m.key.rsplit('.', 1)[-1]}={m.text}" for m in members)
            # the busiest attribute stands for the entity. The union
            # of days would need every chain re-walked for one console line.
            days = max(m.day_count for m in members)
            since = min(m.since for m in members)
            CONSOLE.print(f"  · [bold]{ent}[/] — seen on {days} days since {since}: {attrs}")
        CONSOLE.print("  Promote one with [bold]/promote <key>[/].")
        return ""

    from silica.driver import DRIVER
    from silica.kernel.recall.episodic import promotion_stub
    from silica.kernel.vault_manifest import active_inbox_dir

    # The key names an attribute, the promotion writes the entity it belongs to:
    # `user.dog.name` and `user.dog.breed` are one note about one dog.
    key = keys[0]
    entity = entity_key(key)
    heads = sorted((f for f in store.live_facts() if entity_key(f.key) == entity),
                   key=lambda f: f.key)
    if not heads:
        CONSOLE.print(
            f"  No live episodic chain for [bold]{key}[/] — run /promote to list them."
        )
        return ""
    done = next((h for h in heads if h.promoted), None)
    if done is not None:
        CONSOLE.print(
            f"  [bold]{entity}[/] is already promoted to {done.promoted} — edit that "
            "note, or /nucleate it again to refresh it."
        )
        return ""

    inbox = active_inbox_dir() or "Inbox"
    # The key is model-authored (distiller output), and here it names a file:
    # keep it to one path segment so no key can stage outside the inbox.
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", entity).strip(".-") or "episodic"
    rel = f"{inbox}/{stem}.md"
    DRIVER.upsert(rel, promotion_stub(heads, store=store))
    try:
        target_dir = _pick_target_folder([rel])
    except Exception as exc:
        # No invented folder: the stub is already in the inbox, so the user
        # finishes it with the ordinary verb rather than losing the render.
        logger.debug("/promote: auto-target pick failed: %s", exc)
        CONSOLE.print(
            f"  Could not pick a folder. The stub is at [bold]{rel}[/] — "
            f"run /nucleate {rel} --target=<folder>."
        )
        return ""

    from silica.router.coordinator import Coordinator

    CONSOLE.print(f"  promote: [bold]{entity}[/] → {target_dir}")
    # episodic_capture off: the stub is a render of the store, so distilling it
    # back in would nest the chain inside itself once per promotion.
    # promotion lens: the stub is finished verbatim content — the default
    # authoring lens + 275-char floor rejected every real promotion (55/155/34
    # chars, all no_ops), and the extractive lens skipped every fact as
    # "time-bound personal" (the ingest-direction diversion). The promotion
    # lens selects verbatim, one note per entity, and never re-emits
    # ephemerals; the gate enforces extractivity at the lower floor.
    result = Coordinator(inbox_files=[rel], target_dir=target_dir,
                         episodic_capture=False,
                         distill_profile="promotion").run()
    status = result.get("final_status") or result.get("error") or "done"
    # The FSM names the note, so the ledger CLEANUP appends is the only place
    # the path can be read back from. Last record wins: a re-promotion of the
    # same key overwrites the stamp with the newer note.
    from pathlib import Path

    from silica.kernel.write.provenance import read_records

    notes = list((read_records(Path(rel).name) or [{}])[-1].get("notes") or [])
    # CLEANUP's record lists the run's hub note first ("Life/Life"): the stamp
    # must name the note that holds the facts, not the folder's MOC.

    hub_stem = Path(target_dir.rstrip("/")).name
    entity_notes = [n for n in notes
                    if Path(n).name.removesuffix(".md") != hub_stem]
    notes = entity_notes or notes
    if notes:
        # Re-read: another chunk of the run may have written this store, so the
        # pre-run snapshot in `store` is stale and saving it would erase that.
        ids = [h.id for h in heads]
        store = EpisodicStore()
        # By chain, not by id: the run may have superseded a fact being
        # promoted, and the stamp belongs to the chain, so it follows the head.
        head_of = {link.id: f for f in store.live_facts() for link in store.chain(f)}
        stamped = {head_of[i].id: head_of[i] for i in ids if i in head_of}
        for head in stamped.values():
            head.promoted = notes[0]
        if not stamped:
            CONSOLE.print(f"  wrote {notes[0]}, but the chains for [bold]{entity}[/] "
                          "are gone from the store — not stamped.")
            return ""
        store.save()
        CONSOLE.print(
            f"  promoted: [bold]{entity}[/] ({len(stamped)} chain(s)) → {notes[0]}")
    else:
        CONSOLE.print(f"  promote finished: [bold]{status}[/] — nothing written, "
                      "the chain stays in the queue.")
    return ""


# --- shortcut handlers -------------------------------------------------------
# One per command, module-level and independently readable: the dispatcher below
# is a table, not a 20-branch chain, and `git log -L` on one command no longer
# spans every other. Each takes the tokens AFTER the command word. The two extras
# (`user_input`, `progress`) are passed to every handler and absorbed by `**_`, so
# a handler's signature names exactly what it reads.


def _rejoin_spaced_paths(tokens: list[str]) -> list[str]:
    """Re-join adjacent tokens that only exist as one spaced path.

    An unquoted `/nucleate Inbox/LLM Agent Memory.pdf` shlex-splits into
    three tokens, and each then failed separately with an error naming the
    wrong file ("Skipped Memory.pdf"). Greedy longest-join: a token that
    does not exist on its own but does exist joined with its neighbours
    (vault-relative or cwd-relative) becomes that one path. Tokens that
    exist alone are never merged, so quoted paths keep working unchanged.
    """
    from pathlib import Path as _P

    def _exists(s: str) -> bool:
        vp = CONFIG.vault_path.strip()
        return _P(s).exists() or bool(vp and (_P(vp) / s).exists())

    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("-") or _exists(tok):
            out.append(tok)
            i += 1
            continue
        joined = None
        cand = tok
        for j in range(i + 1, len(tokens)):
            if tokens[j].startswith("-"):
                break
            cand = f"{cand} {tokens[j]}"
            if _exists(cand):
                joined = (j, cand)
        if joined:
            out.append(joined[1])
            i = joined[0] + 1
        else:
            out.append(tok)
            i += 1
    return out

# --- /nucleate: one phase per function --------------------------------------
# The lane is glob -> folder -> plan -> dispatch. Each phase names its inputs and
# its outputs instead of reaching into 400 lines of shared locals, so a change to
# (say) folder expansion no longer has to be read against the dispatch loop.


class _NucleatePlan(NamedTuple):
    """What one /nucleate invocation resolved its arguments into."""

    ready_units: list[tuple[str, list[str]]]   # (label, md files) distillable now
    to_convert: list[str]                      # sources still owed a conversion
    staged: int                                # code notes written inline, no LLM
    needs_agent: bool                          # an argument the flag parser could not read
    # pdf path -> (leaf basename, identity) for a Zotero paper (ADR-0055)
    leaf_for: dict[str, tuple[str, dict]] = {}


def _nucleate_globs(files: list[str]) -> tuple[list[str], bool]:
    """Expand glob tokens against the vault root, and report whether one missed.

    A miss is an answer, not a question for the agent, so the caller stops on it.
    """
    # B6: a glob token ("Inbox/*", the README's documented batch form) is
    # expanded here, against the vault root. Unexpanded it would survive as
    # a literal and fall through to the agent, taking the one batch command
    # the docs advertise off the deterministic path.
    import glob as _glob
    globbed: list[str] = []
    glob_miss = False
    for f in files:
        if any(ch in f for ch in "*?["):
            hits = sorted(
                h.replace(os.sep, "/")
                for h in _glob.glob(f, root_dir=CONFIG.vault_path.strip() or ".")
            )
            if hits:
                CONSOLE.print(f"  {f}: [bold]{len(hits)}[/] match(es)")
            else:
                CONSOLE.print(f"  [yellow]{f}: no files match[/]")
                glob_miss = True
            globbed.extend(hits)
        else:
            globbed.append(f)
    files = globbed
    return files, glob_miss


def _nucleate_expand_folders(
    files: list[str], enabled
) -> tuple[list[str], dict[str, str]]:
    """Expand folder arguments to the files under them.

    Also returns `run_root`: the folder each code file came from, which is how
    the code lane names its destination.
    """
    from pathlib import Path

    from silica.sources.registry import adapter_for, expand_folder, folder_rel
    from silica.tools.atomic import notes_under

    # A folder argument is the common way to say "this subsystem": expand it
    # to the source files under it, then dispatch each exactly like a file.
    # `run_root` remembers which folder each file came from — the code lane
    # names its destination folder after it.
    expanded: list[str] = []
    run_root: dict[str, str] = {}
    for f in files:
        adapter = adapter_for(f, enabled=enabled)
        group = expand_folder(f, enabled) if adapter is None else []
        if group:
            CONSOLE.print(f"  {f}: [bold]{len(group)}[/] source file(s)")
            # run_root is the code lane's destination naming, so it stays
            # keyed on code files only.
            run_root.update(dict.fromkeys(group, folder_rel(f) or ""))
        elif adapter is None:
            # A folder of notes. `expand_folder` cannot see one (git-backed
            # census, and a plain vault is no repo), so this used to fall
            # through to the agent fallback below with nothing but the
            # folder name — a listing an LLM had to guess at.
            group = notes_under(f)
            # An inbox folder of PDFs answered "0 notes" and went to the
            # agent as an unresolvable name. Unconverted files are
            # first-class /nucleate input (each runs through convert below),
            # so a folder argument picks them up like it picks up notes.
            from silica.sources.convert import DOC_EXTS, IMG_EXTS
            from silica.tools.atomic import _unconverted_under
            pending = [
                p for p in _unconverted_under(f)
                if Path(p).suffix.lower() in DOC_EXTS
            ]
            # Images stay batch input only inside the inbox (screenshots
            # dropped there to OCR). In a SOURCE folder they are a book
            # photographed page by page: converting each page as its own
            # document is hundreds of one-page runs of garbage.
            from silica.kernel.recall.paths import is_inbox_path
            if not is_inbox_path(f.rstrip("/") + "/x.md"):
                photos = [p for p in pending
                          if Path(p).suffix.lower() in IMG_EXTS]
                if photos:
                    pending = [p for p in pending if p not in photos]
                    CONSOLE.print(
                        f"  [dim]{f}: left {len(photos)} image(s) alone — "
                        f"a photographed book is one artefact, not "
                        f"{len(photos)} documents; /nucleate an image "
                        f"explicitly to OCR it on its own[/]"
                    )
            if group or pending:
                detail = f"[bold]{len(group)}[/] note(s)" if group else ""
                if pending:
                    detail += (", " if detail else "") + \
                        f"[bold]{len(pending)}[/] file(s) to convert"
                CONSOLE.print(f"  {f}: {detail}")
            group = group + pending
        expanded.extend(group or [f])
    files = list(dict.fromkeys(expanded))
    return files, run_root


def _nucleate_zotero(
    files: list[str], enabled, undo_run
) -> tuple[list[str], dict[str, tuple[str, dict]], int]:
    """Zotero targets resolved to the PDFs they name (ADR-0055 decision 4).

    A paper with a PDF becomes that PDF, so the converter queue, its batch
    resume and its "already nucleated" skip treat it like a dropped file; its
    leaf rides in `leaf_for`. A paper with no PDF is written as its leaf here,
    from title, authors and abstract.
    """
    from silica.sources import zotero
    from silica.sources.registry import adapter_for, stage

    out: list[str] = []
    leaf_for: dict[str, tuple[str, dict]] = {}
    staged = 0
    for f in files:
        if adapter_for(f, enabled=enabled) is not zotero.ZOTERO:
            out.append(f)
            continue
        try:
            # importing_client, not client: a paper with a PDF never reaches
            # ZoteroAdapter.read, so the owner check has to stand here too.
            papers = zotero.resolve(zotero.importing_client(), f)
        except zotero.ZoteroError as exc:
            CONSOLE.print(f"  [yellow]{f}: {exc}[/]")
            continue
        known = zotero.vault_leaves()
        for paper in papers:
            if paper.pdf is None:
                result = stage(zotero.ZOTERO, f"zotero://select/library/items/{paper.key}",
                               "", undo_run)
                if result["status"] == "ok":
                    staged += 1
                    CONSOLE.print(f"  {f}: no PDF in Zotero, wrote [bold]{result['note_path']}[/]")
                else:
                    CONSOLE.print(f"  [yellow]{f}: {result.get('message', '')}[/]")
                continue
            pdf = str(paper.pdf)
            out.append(pdf)
            leaf_for[pdf] = (known.get(paper.key, paper.leaf_name), paper.fields)
    return out, leaf_for, staged


def _nucleate_plan_units(
    files: list[str], enabled, run_root: dict[str, str], undo_run
) -> _NucleatePlan:
    """Triage every resolved file into staged-now, ready-to-distill, or to-convert."""
    from pathlib import Path

    from silica.sources.registry import adapter_for, stage

    files, leaf_for, staged = _nucleate_zotero(files, enabled, undo_run)
    md_files: list[str] = []
    needs_agent = not files  # only flags given (dropped --folder=) → agent infers
    prior_conversions: dict[str, dict] | None = None  # built on first need
    # Pipeline units: loose .md arguments form one ready unit; each source
    # document is its own unit — ready when its segments already exist,
    # queued for conversion otherwise. One Coordinator run per unit, so
    # book N+1 can convert while book N distills.
    ready_units: list[tuple[str, list[str]]] = []
    to_convert: list[str] = []
    for f in files:
        adapter = adapter_for(f, enabled=enabled)
        if adapter is None:
            # No source claims this file type → the converter lane (PDF
            # today). A bare name or folder (no suffix) is a resolvable
            # intent the flag parser couldn't read — agent, not converter.
            if not Path(f).suffix:
                needs_agent = True
                continue
            # Batch resume: conversion is the expensive half (minutes of
            # OCR per scanned book), so a re-run must not pay it again.
            # Both identities are already on disk — segments in the inbox
            # (interrupted run) and the done/ archive (finished book) both
            # carry `source_file` frontmatter.
            from silica.sources.convert import _resolve_input
            try:
                src_key = str(_resolve_input(f))
            except ValueError:
                src_key = ""  # convert() stays the authority on missing files
            if prior_conversions is None:
                prior_conversions = _prior_conversions()
            prior = prior_conversions.get(src_key) if src_key else None
            if prior and prior["inbox"]:
                CONSOLE.print(
                    f"  {f}: reusing [bold]{len(prior['inbox'])}[/] "
                    f"already-converted segment(s)"
                )
                ready_units.append((f, prior["inbox"]))
                continue
            if prior and prior["done"]:
                CONSOLE.print(
                    f"  [dim]{f}: already nucleated "
                    f"({prior['done']} segment(s) in done/) — skipped[/]"
                )
                continue
            to_convert.append(f)
            continue
        result = stage(adapter, f, run_root.get(f, ""), undo_run)
        if result["status"] == "distill":
            md_files.append(f)
        elif result["status"] == "ok":
            staged += 1
            code_ref = result["meta"].get("code_ref", "")
            if len(files) <= 10:  # a whole subsystem would flood the terminal
                # Only a code stub has a code_ref; a mail thread printed "(code_ref )".
                ref = f" (code_ref {code_ref[:8]})" if code_ref else ""
                CONSOLE.print(f"  Wrote [bold]{result['note_path']}[/]{ref}.")
        else:
            CONSOLE.print(f"  [yellow]{f}: {result.get('message', '')}[/]")

    if staged:
        if len(files) > 10:
            CONSOLE.print(f"  Wrote [bold]{staged}[/] code note(s). /wiki for prose.")
        CONSOLE.print("  [dim]/revert undoes this run.[/]")

    # Loose .md arguments distill together, exactly as before the pipeline —
    # first in line: they are ready, so the first conversion overlaps them.
    if md_files:
        ready_units.insert(0, ("", list(dict.fromkeys(md_files))))
    return _NucleatePlan(ready_units, to_convert, staged, needs_agent, leaf_for)


def _nucleate_prepare(
    mfs: list[str], target_dir: str | None, profile: str | None, undo_run
) -> list[str]:
    """refs filter → draft filing → provenance drop, for one unit."""
    # A folder arg can list both a PDF and its already-converted chunks;
    # convert() upserts the same chunk paths, so dedup keeps each once.
    mfs = list(dict.fromkeys(mfs))

    # Apparatus is not content: skip flagged chunks (convert marks them
    # `references: true` / `boilerplate: true`) and Excalidraw drawings.
    # The raw file stays in the inbox for lookup — never venue/journal/ethics
    # notes, never a note made of a drawing's JSON.
    from silica.sources.convert import is_skippable_chunk
    ref_chunks = [mf for mf in mfs if is_skippable_chunk(mf)]
    if ref_chunks:
        mfs = [mf for mf in mfs if mf not in ref_chunks]
        CONSOLE.print(
            f"  [dim]skipped {len(ref_chunks)} non-content file(s) "
            f"(references, contents, venue checklists, Excalidraw drawings) — "
            f"kept in the inbox, not distilled into notes[/]"
        )
        if not mfs:
            CONSOLE.print("  [yellow]nothing left to nucleate: only non-content files were given[/]")
            return []

    # Draft filing (docs/specs/nucleation-forms.md): the owner's own
    # working material is filed, not distilled. Runs before auto-target
    # so a run that was ONLY drafts never pays the folder-pick call. An
    # explicit --profile tops the ladder: no resolution, no filing.
    if not profile:
        mfs = _file_drafts(mfs, target_dir or "", undo_run)
        if not mfs:
            return []  # everything was filed; reported above

    from pathlib import Path as _Path
    from silica.kernel.write.provenance import (
        already_distilled, check_renucleate, content_sha256,
    )

    kept_md: list[str] = []
    distilled_prior = 0
    for mf in mfs:
        try:
            incoming_sha = content_sha256(mf)
            if not incoming_sha:
                kept_md.append(mf)
                continue
            # Same sha as the last COMPLETE record with notes ⇒ this segment
            # is already in the vault; re-distilling it costs a full LLM
            # pass to write nothing (the failure cases are provenance's).
            if already_distilled(_Path(mf).name, incoming_sha):
                distilled_prior += 1
                continue
            modified, prior_notes = check_renucleate(_Path(mf).name, incoming_sha)
            if modified:
                CONSOLE.print(
                    f"  [yellow]re-nucleate of a modified source: {prior_notes} note(s) "
                    f"derived from the previous version[/]"
                )
        except Exception as exc:
            logger.debug("/nucleate: re-nucleate provenance check skipped for %s (non-fatal): %s", mf, exc)
        kept_md.append(mf)
    if distilled_prior:
        CONSOLE.print(
            f"  [dim]{distilled_prior} segment(s) already distilled "
            f"(unchanged since their run) — skipped[/]"
        )
    return kept_md


def _zotero_backlink(leaf: tuple[str, dict], run_id: str | None) -> None:
    """The paper's `Silica · <title>` note in Zotero, after its run: only then
    does the vault hold the notes it lists (ADR-0055 decision 7). Best-effort,
    since the vault side of the import has already succeeded."""
    from silica.kernel.vault_manifest import get_active_manifest
    from silica.sources import zotero, zotero_write

    link = get_active_manifest().zotero
    if link is None or not link.backlinks:
        return
    try:
        status = zotero_write.backlink(zotero.client(), leaf[1]["zotero_key"],
                                       _leaf_rel(leaf[0]), run_id=run_id or "")
    except zotero.ZoteroError as exc:
        CONSOLE.print(f"  [yellow]Zotero: back-link note not written: {exc}[/]")
        return
    if status == "edited":
        CONSOLE.print("  Zotero: the back-link note was edited in Zotero, so it was left as it is")
    else:
        CONSOLE.print(f"  Zotero: back-link note {status} · in zotero.jsonl · undo: /revert")


def _leaf_rel(name: str) -> str:
    from silica.kernel.recall.paths import SOURCES_DIR
    from silica.kernel.vault_manifest import in_write_dir

    return f"{in_write_dir(SOURCES_DIR)}/{name}"


def _leaf_text(name: str) -> str | None:
    from silica.driver import DRIVER

    try:
        return DRIVER.read_note(_leaf_rel(name)).content
    except Exception:
        return None  # no leaf yet: /revert deletes the one convert creates


def _nucleate_result_line(result: dict) -> str:
    """The one-line outcome of a Coordinator run, ready to print after the label.

    `label` inside the coverage comprehension is the counter's name, deliberately
    not the caller's unit label — extracting this is what stops the two sharing
    a name in one scope.
    """
    status = result.get("final_status") or result.get("error") or "done"
    failed = result.get("failed_chunks") or []
    extra = f" — {len(failed)} chunk(s) failed" if failed else ""
    sw = result.get("link_sweep") or {}
    if sw.get("links_stripped"):
        extra += (
            f" — {sw['links_stripped']} dangling link(s) unlinked "
            f"in {sw['notes_edited']} note(s)"
        )
    if sw.get("links_relinked"):
        extra += f" — {sw['links_relinked']} spelling variant(s) repointed"
    # Coverage, post-anneal. Silence here read as "everything landed",
    # and the counts that said otherwise lived in log.md and a bundle
    # under ~/.silica — neither of which anyone opens after a Success.
    cov = result.get("coverage") or {}
    bits = [
        f"{cov[k]} {label}" for k, label in (
            ("recovered_ops", "deferred op(s) recovered"),
            ("deferred_ops", "still deferred"),
            ("flagged_notes", "note(s) landed flagged for review"),
        ) if cov.get(k)
    ]
    if bits:
        extra += " — " + ", ".join(bits)
    # A re-nucleated source keeps its previous derivation beside the new one;
    # the line says so and names the way back, since no prompt stood in the way.
    for src, n in sorted((result.get("renucleated") or {}).items()):
        extra += (f" — {src} re-nucleated: {n} note(s) from its previous version "
                  f"kept (/revert --source {src} removes them)")
    return f"[bold]{status}[/]{extra}"


def _sc_nucleate(args: list[str], *, user_input: str = "", **_) -> str | None:
    """/nucleate <file|folder|glob...> [--target=DIR] [--hub=H] [--profile=P]
    [--seen=YYYY-MM-DD] [--no-keep-sources] — ingest sources into vault notes.

    No arguments drains the write-ahead log instead. Fully inline: returns "" when
    it dispatched, or an agent-directed message when the target cannot be resolved
    from the arguments alone.
    """
    args = _rejoin_spaced_paths(args)
    if not args:
        return _drain_wal()
    files = _positional(args)  # preserve original case
    target_dir = _str_flag(args, "--target")
    hub = _str_flag(args, "--hub")
    seen = _str_flag(args, "--seen")  # capture clock: the day the events happened
    # Explicit lens override: tops the whole form ladder
    # (docs/specs/nucleation-forms.md), filing included.
    profile = _str_flag(args, "--profile")
    # On by default: the leaf lives in `sources/`, which is
    # retrieval-invisible by construction (is_source_leaf excludes it from
    # search, search_context and embeddings), so it costs disk and nothing
    # else — and it is what makes a note's verbatim source reachable at all
    # (reliability_tier reads exactly that). --no-keep-sources opts out.
    keep_sources = "--no-keep-sources" not in args

    if seen:
        # Trust boundary: this string becomes the valid_from on every claim
        # of the run — a typo'd date would poison note_clock vault-wide.
        import datetime as _dt
        try:
            seen = _dt.date.fromisoformat(seen).isoformat()
        except ValueError:
            CONSOLE.print(f"  [yellow]--seen ignored: {seen!r} is not YYYY-MM-DD[/]")
            seen = ""

    from silica.kernel.vault_manifest import get_active_manifest
    from silica.sources.convert import convert
    from silica.kernel.write.undo_journal import get_undo_journal

    files, glob_miss = _nucleate_globs(files)
    if not files and glob_miss:
        return ""  # a miss is an answer, not a question for the agent

    enabled = get_active_manifest().sources
    files, run_root = _nucleate_expand_folders(files, enabled)

    # One CLI journal run per /nucleate invocation for what the planner writes
    # itself (staged code, filed drafts). The FSM opens its own journal run per
    # dispatched unit, so on a multi-book batch /revert undoes one book at a
    # time, most recent first — run it again for the previous one.
    undo_run = get_undo_journal().start_run(
        source="nucleate", vault=CONFIG.vault_path.strip() or None
    ) if files else None
    ready_units, to_convert, staged, needs_agent, leaf_for = _nucleate_plan_units(
        files, enabled, run_root, undo_run)

    if not ready_units and not to_convert:
        if staged or not needs_agent:
            # Staged inline, or only genuinely-unsupported files — nothing for the agent.
            return ""
        # A dropped --folder=, a directory arg, or connective words the flag
        # parser can't read. Hand the raw line to the agent so it infers intent
        # (it already holds the tools + the vault map).
        return (
            f"The user typed {user_input!r} to nucleate/ingest, but no ingestible "
            "file was resolved. The argument may be a folder (call silica_files "
            "with folder= and nucleate both its notes and its \"code\" entries — "
            "a code folder holds no .md and is still ingestible), a single note, or carry a "
            "--target/--folder the flag parser missed. Resolve the inbox file(s), "
            "then call silica_run_injector with the resolved inbox_files and "
            "target_dir. If nothing is ingestible, say so briefly."
        )

    total_units = len(ready_units) + len(to_convert)
    batch = total_units > 1
    dispatched = 0

    def _at() -> str:
        """Clock on the per-unit lines of a batch — a folder of scanned
        books runs for hours, and the unit boundaries are the only place
        to read where the time went. Single-unit runs stay unadorned."""
        from datetime import datetime
        return f"[dim]{datetime.now().strftime('%H:%M:%S')}[/] " if batch else ""

    # Conversions run with the user-passed destination, as they always did:
    # an auto-picked target is resolved at first dispatch, after conversion.
    convert_dest = target_dir

    def _dispatch_unit(label: str, mfs: list[str]) -> str | None:
        """Prepare and run one unit; a returned string is the agent
        fallback (unresolvable target) and ends the whole batch."""
        nonlocal target_dir, dispatched
        mfs = _nucleate_prepare(mfs, target_dir, profile, undo_run)
        if not mfs:
            return None

        if not target_dir:
            # auto-target: one small folder-pick call, not a full agent
            # turn; resolved once, every later unit inherits it.
            try:
                target_dir = _pick_target_folder(mfs)
                CONSOLE.print(f"  auto-target: [bold]{_announced_target(target_dir)}[/]")
            except Exception as exc:
                logger.debug("/nucleate: auto-target pick failed (non-fatal): %s", exc)

        if not target_dir:
            # Fallback: hand the folder choice to the agent (legacy behavior).
            files_json = json.dumps(mfs)
            msg = (
                f"Run the Injector pipeline for {len(mfs)} file(s).\n"
                f"No target folder was given. Skim the inbox file(s) {files_json}, "
                f"then pick the single most relevant existing vault folder for "
                f"this content (use the vault map; list folders if unsure). If "
                f"nothing fits, pick a sensible new folder name. State the chosen "
                f"folder in one line, then call `silica_run_injector` with "
                f"inbox_files={files_json}, target_dir=<chosen folder>"
            )
            if hub:
                msg += f", hub={json.dumps(hub)}"
            return msg + "."

        # Direct FSM dispatch — no LLM orchestrator. The old path
        # round-tripped the whole session history through the model on
        # every turn just to relay these arguments to silica_run_injector
        # (~40% of a nucleate run's tokens for a handful of decision tokens).
        from silica.router.coordinator import Coordinator

        head = f"{label}: " if (batch and label) else ""
        CONSOLE.print(
            f"  {_at()}{head}nucleate: {len(mfs)} file(s) → [bold]{_announced_target(target_dir)}[/]"
        )
        try:
            result = Coordinator(
                inbox_files=mfs, target_dir=target_dir, hub=hub or None,
                keep_sources=keep_sources, seen_override=seen or None,
                distill_profile=profile or None,
            ).run()
        except ValueError as exc:
            # A path outside the vault (or any other rejected argument) is
            # user error, not a crash: the batch moves to the next unit.
            CONSOLE.print(f"  [yellow]nucleate: {exc}[/]")
            return None
        CONSOLE.print(
            f"  {_at()}{head}nucleate finished: "
            f"{_nucleate_result_line(result)} — details in log.md"
        )
        dispatched += 1
        return None

    # Conversion is local OCR, distillation is network LLM — different
    # resources, so the NEXT book converts while the current unit distills.
    # One worker, one conversion ahead: further parallel OCR just contends
    # for the same GPU. Worth ~2% of wall-clock, not the 2x this comment
    # used to claim: measured 2026-08-16 on 07-religioni-comparate, OCR is
    # 43-68 s per book against 6+ min of distillation. The 2x ceiling needs
    # a corpus where OCR time approaches LLM time, which text PDFs never do.
    from concurrent.futures import ThreadPoolExecutor

    pool = ThreadPoolExecutor(max_workers=1) if to_convert else None
    leaf_prior: dict[int, str | None] = {}

    def _submit(i: int):
        if pool is None or i >= len(to_convert):
            return None
        if batch:
            CONSOLE.print(
                f"  {_at()}[{i + 1}/{len(to_convert)}] converting {to_convert[i]}"
            )
        leaf = leaf_for.get(to_convert[i])
        if leaf and undo_run:
            # Read before the conversion writes it: a re-imported paper's
            # leaf must be restored by /revert, not deleted.
            leaf_prior[i] = _leaf_text(leaf[0])
        if leaf:
            return pool.submit(convert, to_convert[i], convert_dest, leaf=leaf)
        return pool.submit(convert, to_convert[i], convert_dest)

    pending = _submit(0)
    try:
        for label, segs in ready_units:
            msg = _dispatch_unit(label, segs)
            if msg:
                return msg
        for i, src in enumerate(to_convert):
            try:
                segs = pending.result()
            except (ValueError, RuntimeError) as e:
                CONSOLE.print(f"  [yellow]Skipped {src}: {e}[/]")
                segs = []
            if i in leaf_prior and segs and undo_run:  # leaf_prior fills only under an undo_run
                from silica.sources.registry import _record_inverse
                _record_inverse(undo_run, _leaf_rel(leaf_for[src][0]), leaf_prior[i])
            pending = _submit(i + 1)
            if segs:
                before = dispatched
                msg = _dispatch_unit(src, segs)
                if msg:
                    return msg
                if src in leaf_for and dispatched > before:
                    _zotero_backlink(leaf_for[src], undo_run)
    finally:
        if pool is not None:
            # wait=False: a mineru run cannot be cancelled; on an early
            # agent-fallback return the in-flight conversion finishes in
            # the background and its segments are reused next time.
            pool.shutdown(wait=False)

    if total_units and not dispatched:
        CONSOLE.print(
            "  [dim]nothing left to distill — everything was filed, "
            "skipped, or already in the vault[/]"
        )
    if batch and dispatched > 1:
        CONSOLE.print(
            f"  [dim]{dispatched} run(s) — /revert undoes the most recent; "
            f"run it again for the one before[/]"
        )
    return ""


def _sc_settings(args: list[str], **_) -> str | None:
    """/settings [<key> <value|none>] — read or edit vault.yaml without the wizard."""
    # View/edit vault.yaml without the wizard. ponytail: safe_dump rewrite —
    # YAML comments are not preserved; hand-edit the file to keep them.
    import yaml as _yaml
    from pathlib import Path as _P
    from silica.kernel.vault_manifest import MANIFEST_REL, reset_manifest_cache
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
    from silica.kernel.recall.paths import atomic_write_bytes
    atomic_write_bytes(mf, _yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False).encode("utf-8"))
    reset_manifest_cache()
    CONSOLE.print(f"  {key} = {raw} → {mf.name} (comments in the file are not preserved)")
    return ""


def _sc_convert(args: list[str], **_) -> str | None:
    """/convert <file...> [--target=DIR] — document to inbox notes, no distillation."""
    args = _rejoin_spaced_paths(args)
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
    from silica.tools.graph import _in_folder

    folder = next(iter(_positional(args)), "")

    # list_files(folder) pre-filters loosely (startswith); _in_folder tightens
    # it so /enrich Foo never leaks into a sibling FooBar/ folder.
    paths = [r.path for r in DRIVER.list_files(folder=folder) if _in_folder(r.path, folder)]
    if not paths:
        return f"Error: no files found in '{folder}'."

    payloads = [{"note_paths": chunk} for chunk in _chunk_by_json_size(paths)]
    return _seed_batch_ledger("silica_enrich_batch", payloads, kind="enrich", label=folder or "vault")


def _sc_dedup(args: list[str], **_) -> str | None:
    """/dedup [folder] — seed a batch ledger over the near-duplicate pairs found."""
    from silica.tools.runners import _scan_dedup_pairs

    folder = " ".join(_positional(args))

    pairs, err = _scan_dedup_pairs(folder)
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
        from silica.kernel.recall.paths import to_vault_relative
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
        f"3. Then STOP and ask whether to begin; `/learn {name}` continues the plan at its "
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
            return "Error: /learn --teach requires a note. Usage: /learn --teach <note>"
        return _learn_teach_prompt(teach)
    plan = _kv_flag(args, "--plan")
    scope = _str_flag(args, "--scope")
    steps = _str_flag(args, "--steps")
    if plan is not None:
        if not plan:
            return "Error: /learn --plan requires a name. Usage: /learn --plan <name> --scope=<folder|note,note|topic:term> [--steps=<note>]"
        if steps:
            return _learn_plan_prompt(plan, "steps", steps, restrict=scope)
        if not scope:
            return "Error: /learn --plan needs --scope=<folder|note,note|topic:term> or --steps=<note>"
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
    The directive's head is what opens the asked gate, and the model still
    passes the untainted one (ADR-0076 §5)."""
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
    "/promote": lambda args, **_: _promote(args),
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


def _expand_workflow_shortcut(user_input: str, progress=None) -> str | None:
    """Expand workflow shortcuts (e.g. /report, /nucleate) into agent-directed messages.

    Returns the expanded message string, or None if the input is not a
    recognised shortcut. Expanded messages flow through the normal agentic
    loop so the agent calls the tools and follows the steering protocol.

    `progress` is the REPL's own renderer, for the shortcuts that drive work
    inline. A `_ProgressRenderer` claims the module-global batch hook and
    subscribes to the BUS for its whole life, so a second one built here would
    orphan the REPL's — reuse it when the caller has one.
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
    return handler(parts[1:], user_input=user_input, progress=progress)


def _handle_slash_command(cmd: str, messages: list[dict]) -> bool | None:
    """Handle a meta slash command. True = handled, False = exit the REPL,
    None = not a recognized command (the caller hands it to the agent)."""
    cmd = cmd.strip().lower()

    if cmd in ("/exit", "/quit", "/q"):
        return False  # Signal to exit

    if cmd.startswith("/sessions"):
        from silica.agent import narration as _narr
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
        from silica.agent import narration as _narr
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
        _update_context_tokens(messages)
        CONSOLE.print(f"  resumed [bold]{(row or {}).get('title', sid)}[/] "
                      f"({len(replayed)} message(s))")
        return True

    if cmd == "/model":
        if not CONFIG.model:
            CONSOLE.print("  Current model: [bold](not configured)[/]")
            return True
        from silica.agent.providers import model_limits
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
    (live_probe) so `silica_doctor(live=True)` over MCP is the same check —
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
    if not sys.stdin.isatty() or os.getenv("SILICA_WIZARD_DONE") == "1":
        return
    import silica.onboarding.wizard as wizard_mod
    if wizard_mod.run_wizard() != 0:
        return  # aborted / failed → no re-exec, fall back to the hint
    # re-exec rather than reload — CONFIG is a module-level singleton
    # imported by value across the codebase, so reassigning it wouldn't reach
    # those aliases. execve inherits the wizard's os.environ updates; the guard
    # env var stops an infinite relaunch if config still doesn't resolve.
    os.environ["SILICA_WIZARD_DONE"] = "1"
    os.execve(sys.executable, [sys.executable, *sys.argv], os.environ)


def _resolve_context_budget() -> None:
    """Size the REPL context meter to the live model's window.

    SILICA_MAX_CONTEXT pins the window for LOCAL providers (LM Studio, Ollama),
    whose served window silica can't reliably introspect. Hosted providers
    (OpenRouter) report their own context_length, so the pin is ignored there
    and the provider's value always wins. Falls back to the static default when
    the provider is unreachable.
    """
    if not _model_configured():
        return
    if os.getenv("SILICA_MAX_CONTEXT") and CONFIG.provider != "openrouter":
        return
    from silica.agent.providers import model_limits
    window, _ = model_limits(CONFIG.provider, CONFIG.model)
    if window:
        CONFIG.max_context_tokens = window


def _ensure_servers() -> None:
    """Start the local model servers named in the env. No-op when none are."""
    from silica.onboarding.serve import ensure_local_servers
    ensure_local_servers()


_CLI_HELP = """\
silica — your personal note curator agent

Usage:
  silica                     open the REPL in the current folder's vault
  silica --gui [--port N]    serve the web GUI (default http://localhost:8765)
  silica doctor [--live]     check the environment (--json for machine output)
  silica init [--advanced]   first-run wizard
  silica setup <client>      register the MCP server (claude, codex, opencode, dsh)
  silica connect             bridge to the Obsidian desktop app
  silica mcp [--extended|--all] [--vault DIR]
                             serve a vault over stdio MCP (default: the
                             folder it starts in; --vault pins another one)
  silica hook <event>        harness hook producer (SessionStart); stdin = payload
  silica import <path>       import external material into the vault
  silica update [--check]    self-update
  silica --version           print the version and exit

Options:
  -c "<command>"             run one REPL command and exit (repeatable),
                             e.g. silica -c "/report"
  -v, --verbose              debug logging
  -h, --help                 this help

The vault is the folder you launch in; SILICA_VAULT overrides it. Type /help
inside the REPL for the slash-command reference."""


def _dispatch_subcommand(args: list[str]) -> int | None:
    """Handle `silica doctor` / `init` / `setup` / `connect` / `mcp` / `update`.

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
    if args[:1] == ["capture"]:
        # Claude Code hook producer. No vault bootstrap: the vault comes from
        # the hook's own cwd (walk-up), not from this process's config, and
        # the whole point is to stay silent and fast inside someone else's
        # session. Fail-open covers the import too, not just the body.
        try:
            import silica.capture as capture_mod
            return capture_mod.run_capture(sys.stdin.read())
        except Exception:
            return 0
    if args[:1] == ["hook"]:
        # Same contract as capture above: the vault comes from the payload's
        # cwd, and fail-open covers the import too.
        try:
            import silica.hook as hook_mod
            return hook_mod.run_hook(args[1:], sys.stdin.read())
        except Exception:
            return 0
    if args[:1] == ["import"]:
        # Dispatch runs before main()'s setup (like connect/mcp below) — the
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
    if args[:1] == ["setup"]:
        import silica.onboarding.setup_client as setup_mod
        return setup_mod.run_setup(args[1:])
    if args[:1] == ["connect"]:
        # Dispatch runs before main()'s setup (unlike --gui) — do it here.
        _activate_repo_mode()
        _announce_code_lane()
        from silica.kernel.vault_manifest import apply_manifest_to_config
        apply_manifest_to_config()
        _resolve_context_budget()
        _setup_logging(debug="--verbose" in sys.argv or "-v" in sys.argv or CONFIG.debug_logging)
        _ensure_servers()
        import silica.ui.connect as connect_mod
        return connect_mod.run_connect()
    if args[:1] == ["mcp"]:
        # Same bootstrap as connect, minus the REPL context meter (no agent
        # loop behind MCP tools). stdio transport: stdout is the protocol
        # channel, so plain stderr logging instead of _setup_logging's console,
        # and the bootstrap banner is diverted too (rich resolves sys.stdout per
        # write, so redirecting it here is enough). The redirect must NOT wrap
        # run_mcp: that is where stdout has to be the real protocol stream.
        import contextlib
        import silica.ui.mcp as mcp_mod
        opts = mcp_mod.parse_cli_args(args[1:])
        if opts["error"]:
            print(opts["error"], file=sys.stderr)
            return 2
        with contextlib.redirect_stdout(sys.stderr):
            if opts["vault"]:
                # Explicit per-server pin: one server entry per vault in the
                # client config is multi-vault reach. switch_vault is the whole
                # sequence (driver, caches, manifest — not an assignment) and
                # already applies the manifest, so the cwd branch below is not
                # repeated here.
                sw = switch_vault(opts["vault"])
                if sw.error:
                    print(f"--vault {opts['vault']}: {sw.error}", file=sys.stderr)
                    return 2
                print(f"  Vault: {sw.vault}")
            else:
                _activate_repo_mode()
                from silica.kernel.vault_manifest import apply_manifest_to_config
                apply_manifest_to_config()
            _ensure_servers()
        logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
        _attach_file_log()
        return mcp_mod.run_mcp(all_tools=opts["all_tools"], extended=opts["extended"])
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
    """Parse `--port N` / `--port=N` from argv (default 8765)."""
    for i, a in enumerate(sys.argv):
        raw = a.split("=", 1)[1] if a.startswith("--port=") else (
            sys.argv[i + 1] if a == "--port" and i + 1 < len(sys.argv) else None
        )
        if raw is not None:
            try:
                return int(raw)
            except ValueError:
                pass
    return 8765


def main():
    """Entry point for the `silica` CLI command."""
    _args = [a for a in sys.argv[1:] if a not in ("--verbose", "-v")]
    code = _dispatch_subcommand(_args)
    if code is not None:
        sys.exit(code)
    _activate_repo_mode()
    _announce_code_lane()
    from silica.kernel.vault_manifest import apply_manifest_to_config
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
    # This session's own identity, for the capture lane. Random, not a clock:
    # two silica processes started in the same second share the same vault WAL,
    # and a deterministic name would have one overwrite the other's envelope.
    # capture_session is opt-in and fail-open in itself, so both call sites
    # below are the bare call — no wrapper, nothing to guard.
    from silica.capture import capture_session
    session_id = uuid.uuid4().hex[:12]

    from silica.ui.renderer import make_progress_callback
    callback = make_progress_callback()

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

        # Direct shortcuts bypass the LLM entirely (case-sensitive args preserved)
        if user_input.startswith("/") and _handle_direct_shortcut(user_input, messages):
            continue

        # Expand workflow shortcuts (/report, /nucleate etc.) into agent-directed messages
        is_directive = False
        try:
            expanded = _expand_workflow_shortcut(user_input, progress=callback)
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
                from silica.agent import narration as _narr
                _narr.NARRATOR.close()          # next user turn opens a fresh sid
                session_id = uuid.uuid4().hex[:12]
                continue

            result = _handle_slash_command(user_input, messages)
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

        # Normal user message → agentic loop. CLI-expanded shortcuts carry an
        # `origin` so the wire boundary (and our own bookkeeping) can tell a
        # harness directive apart from a human turn.
        msg: dict = {"role": "user", "content": user_input}
        if is_directive:
            msg["origin"] = "cli"
        messages.append(msg)

        # Born at the first user turn (spec §5); idempotent afterwards. The
        # sid is capture's session_id so the two records share one id space.
        from silica.agent import narration as _narr
        _narr.NARRATOR.ensure_session(driver="tui", sid=session_id)
        session_id = _narr.NARRATOR.sid or session_id   # /resume may have switched it
        _narr.NARRATOR.turn(msg)

        # Both wrappers forward every event to the renderer untouched: WebTurn
        # records the trace the citations are built from, RecallWatch counts
        # recall misses for the thin-coverage hint.
        watch = WebTurn(web[0], callback) if web else RecallWatch(callback)

        try:
            answer = run_agent(
                messages,
                model=CONFIG.model,
                tool_progress_callback=watch,
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
            if web:
                answer = watch.attribute(answer, messages)
            elif watch.web_answer:
                from silica.sources.web_research import relay_sources

                answer = relay_sources(answer, messages)
            # Final-assistant turn beat, post-attribution (see loop.py): the
            # replay must carry what the user actually read, sources included.
            if messages and messages[-1].get("role") == "assistant":
                _narr.NARRATOR.turn(messages[-1])
            if answer:
                CONSOLE.print()
                CONSOLE.print("[role.assistant]⏺ silica[/]")
                CONSOLE.print(FlatMarkdown(answer))
                CONSOLE.print()
            if not web and watch.thin:
                CONSOLE.print(f"  [dim]{THIN_COVERAGE_HINT}[/]\n")
            # run_agent already appended the final assistant message to the
            # history — re-appending `answer` here would store it twice.
            _update_context_tokens(messages)
            collapsed = _compact_context(messages, collapsed)
        except KeyboardInterrupt:
            callback.close()
            from silica.agent import narration as _narr
            _narr.NARRATOR.cancel(driver="tui", target=None, scope="turn")
            CONSOLE.print("\n  [dim](interrupted)[/]")
        except Exception as e:
            callback.close()
            logger.exception("Agent error")
            CONSOLE.print(f"\n  [bold red]Error:[/] {e}\n")

    # Every exit from the REPL passes here: /exit, Ctrl+D, Ctrl+C at the prompt.
    if not user_memory.incognito():
        capture_session(messages, session_id=session_id, driver="tui")
    from silica.agent import narration as _narr
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
