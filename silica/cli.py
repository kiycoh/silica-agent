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
import re
import shlex
import sys
import uuid
from typing import NamedTuple

from silica.ui.style import FlatMarkdown

from silica.agent.constraints import AgentConstraints, chat_tools, web_turn_constraints
from silica.agent.loop import run_agent
from silica.agent.recall_watch import THIN_COVERAGE_HINT, RecallWatch
from silica.config import CONFIG
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
import silica.sources.web_research  # noqa: F401  (registers the web_search and web_fetch tools)
from silica.sources.web_research import WebTurn

logger = logging.getLogger(__name__)


def _count_context_tokens(messages: list[dict]) -> int:
    """Pure counter — lets callers (e.g. the web seed prewarm) count a candidate
    message list without clobbering the live session's CONFIG.context_tokens."""
    try:
        import litellm
        return litellm.token_counter(model=CONFIG.model, messages=messages)
    except Exception:
        return sum(len(m.get("content") or "") for m in messages) // 4


def _update_context_tokens(messages: list[dict]) -> None:
    CONFIG.context_tokens = _count_context_tokens(messages)


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


def _vault_scope() -> str:
    """One line naming the two paths the agent must not confuse.

    Reads span the whole vault; new notes are confined to `write_dir`. Without
    this the model reads "vault" as the folder it writes in and reports an empty
    vault while sitting on a repo full of Markdown.
    """
    from silica.kernel.vault_manifest import active_write_dir

    vault = CONFIG.vault_path
    write_dir = active_write_dir()
    if not write_dir:
        return f"Vault: {vault} — you read and write notes anywhere under it."
    return (
        f"Vault: {vault} — you read everything under it, including files that "
        f"are not yours (a repo's own README, docs, specs). New notes go under "
        f"{write_dir}/, the only place you may write; that folder being empty "
        f"does not mean the vault is empty."
    )


def _fresh_messages() -> list[dict]:
    """Seed a fresh conversation: system prompt + vault scope + map + token count.

    Single source of truth for the initial state, shared by session start and
    /clear so the two can't drift.
    """
    from silica.kernel.vault_manifest import get_active_manifest

    conv = get_active_manifest().conventions
    reply = conv.reply_language or conv.language
    messages: list[dict] = [{"role": "system", "content": system_prompt(reply)}]
    messages.append({"role": "system", "content": _vault_scope()})
    _inject_vault_map(messages)
    # The vault map is the vault's own language. On a vault whose notes are not
    # in `reply`, that bulk drowns the language rule sitting in message 0, and
    # the model answers in the notes' language. Restate it last, closest to the
    # user turn.
    messages.append({"role": "system", "content": _lang_prefer(reply)})
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


def default_user_vault(home=None):
    """Stable per-user vault used when no explicit SILICA_VAULT and no repo
    mode applies. Sits alongside ~/.silica/{ledger,undo_journal,checkpoints}.db.
    """
    from pathlib import Path

    return (home or Path.home()) / ".silica" / "vault"


def resolve_cwd_vault(cwd, home=None):
    """Pure resolver for the vault a `silica` launched in `cwd` curates.

    Returns the directory to adopt, or None when this place is not a vault and
    the caller should fall back. The shell already says which vault you mean, so
    the working directory decides — a SILICA_VAULT constant in a .env would
    otherwise follow you into every other project.

    - inside a git repo → the repo root (one project is one vault, from any of
      its subdirectories);
    - anywhere else → cwd itself;
    - $HOME or the filesystem root → None: a vault is a folder of notes, not
      everything you own. The root is not reachable by launching a shell there
      but a GUI client can spawn a stdio server with cwd ``/``, and indexing
      the whole disk is never what that meant.

    Adoption of the returned path (a pre-existing ``docs/silica`` under it still
    wins for back-compat) belongs to ``resolve_vault_switch``; where writes may
    land inside it is the separate `write_dir` axis (`onboarding.adopt`).
    """
    from pathlib import Path
    from silica.kernel.code import gitstate

    cwd = Path(cwd).resolve()
    if cwd == Path(home or Path.home()).resolve() or cwd == Path(cwd.anchor):
        return None
    root = gitstate.find_repo_root(cwd)
    if root is None:
        return str(cwd)
    return str(Path(root).resolve())


def _activate_repo_mode() -> None:
    """Side-effecting startup vault selection: the working directory wins.

    An *exported* SILICA_VAULT outranks it (`config.VAULT_PINNED` — the pin for
    headless runs like cron, which start wherever the scheduler put them); one
    read from a .env file does not. Where cwd is not a vault ($HOME), SILICA_VAULT
    is the fallback, then a stable ~/.silica/vault.

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
        /map <nota> [--force]
        /find <query> [--k=N]
        /impact [<git-range>]
        /path <noteA> <noteB>
        /contested
        /undo [note-path]
    """
    from silica.tools import TOOLS

    parts = raw_input.strip().split()
    if not parts:
        return False
    cmd = parts[0].lower()

    if cmd == "/vault":
        from pathlib import Path
        from silica.driver import reset_driver

        arg = " ".join(parts[1:]).strip()
        if arg:
            target = resolve_vault_switch(arg)
            if target.error:
                CONSOLE.print(f"  [red]Cannot adopt as a vault — {target.error}[/]")
                return True
            if target.created:
                Path(target.vault).mkdir(parents=True, exist_ok=True)
                CONSOLE.print(f"  Created [bold]{target.vault}[/] as the session vault.")
            resolved = target.vault
            from silica.onboarding.adopt import declare_write_dir, seed_silicaignore

            declared = declare_write_dir(resolved)
            if declared:
                CONSOLE.print(
                    f"  Source tree — writes confined to [bold]{declared}/[/]; the rest of "
                    "the vault is read-only context. Change `write_dir` in vault.yaml."
                )
            if seed_silicaignore(resolved):
                CONSOLE.print("  Created [bold].silicaignore[/] — add folders to keep out of the index.")
            CONFIG.vault_path = resolved
            reset_driver()
            from silica.kernel.text.overlay import reset_overlay_cache
            reset_overlay_cache()  # overlay is vault-scoped; don't serve the old vault's
            from silica.kernel.vault_manifest import apply_manifest_to_config, reset_manifest_cache
            reset_manifest_cache()  # manifest is vault-scoped too
            apply_manifest_to_config()
            from silica.kernel.vault_manifest import get_active_manifest

            if get_active_manifest().write_dir is None:
                # Declared but unresolvable (absolute/traversal). Refusing here is
                # the whole point of not degrading it to "" in the parser.
                CONSOLE.print(
                    "  [red]⚠ vault.yaml declares an invalid `write_dir` — every write "
                    "will be rejected until it is a relative path inside the vault.[/]"
                )
            # Vault-scoped store caches are path-keyed (harmless on lookup) but
            # retain the old vault's index/vectors for the process lifetime.
            from silica.kernel.recall.relatedness import reset_vault_caches
            reset_vault_caches()
            CONSOLE.print(f"  Vault → [bold]{resolved}[/] (backend: {CONFIG.backend})")
            _announce_code_lane()
            # Surface the frozen-language drift here, not only in `/vault` info:
            # a switch is exactly when a wrong-frozen store (english on an IT
            # vault) would otherwise stay silent. Reuses the doctor's check.
            from silica.onboarding.checks import language_status

            lang, store_lang, drift = language_status(resolved)
            if drift:
                CONSOLE.print(
                    f"  [yellow]⚠ Language: {lang}, co-occurrence store "
                    f"frozen {store_lang} — run /cooccur --force to rebuild.[/]"
                )
            CONSOLE.print(
                "  [dim]Index namespace follows the vault — run /embed and /cooccur "
                "if this vault has not been indexed yet.[/]"
            )
            return True
        vault = CONFIG.vault_path or "(not configured)"
        CONSOLE.print(f"  Vault:   [bold]{vault}[/]")
        CONSOLE.print(f"  Backend: {CONFIG.backend}")
        if CONFIG.vault_path:
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

    if cmd == "/status":
        run_id = parts[1] if len(parts) > 1 else ""
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

    if cmd in _REFRESH:
        tool, label = _REFRESH[cmd]
        folder = ""
        for part in parts[1:]:
            if part.startswith("--folder="):
                folder = part[len("--folder="):]
            elif not part.startswith("-"):
                folder = part
        result = TOOLS[tool].run(folder=folder, force="--force" in parts[1:])
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

    if cmd == "/wiki":
        args = parts[1:]
        folder = next((a for a in args if not a.startswith("-")), "") or None
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

    if cmd == "/graph":
        output_path = "graph.html"
        folder = ""
        positional = [p for p in parts[1:] if not p.startswith("-")]
        if positional:
            output_path = positional[0]
        if len(positional) > 1:
            folder = positional[1]
        result = TOOLS["silica_graph_export"].run(output_path=output_path, folder=folder)
        try:
            parsed = json.loads(result)
            CONSOLE.print(f"  Graph written to: [bold]{parsed.get('output_path', output_path)}[/]")
        except Exception:
            CONSOLE.print(result)
        return True

    if cmd == "/map":
        force = "--force" in parts[1:]
        positional = [p for p in parts[1:] if not p.startswith("-")]
        note = " ".join(positional).strip()
        if not note:
            CONSOLE.print("  Usage: /map <nota> [--force]")
            return True
        result = TOOLS["silica_mindmap"].run(note_path=note, force=force)
        try:
            parsed = json.loads(result)
            if parsed.get("skipped"):
                CONSOLE.print(
                    f"  [yellow]Mappa già presente[/] ({parsed['skipped']}) — "
                    "non sovrascritta. Usa [bold]/map <nota> --force[/] per rigenerare."
                )
            elif "error" in parsed:
                CONSOLE.print(f"  [red]{parsed['error']}[/]")
            else:
                CONSOLE.print(
                    f"  Mappa scritta: [bold]{parsed.get('path', '?')}[/] "
                    f"({parsed.get('nodes', '?')} nodi, {parsed.get('edges', '?')} archi)"
                )
        except Exception:
            CONSOLE.print(result)
        return True

    if cmd == "/find":
        k = _int_flag(parts[1:], "--k=", 5)
        # original case preserved — raw_input, not the lowered cmd
        query = " ".join(p for p in parts[1:] if not p.startswith("-"))
        if not query:
            CONSOLE.print("  Usage: /find <query> [--k=N]")
            return True
        result = TOOLS["silica_semantic_search"].run(query=query, k=k)
        try:
            parsed = json.loads(result)
            results = parsed.get("results", [])
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

    if cmd == "/stale":
        from pathlib import Path
        from silica.kernel.code import codedocs
        vault = CONFIG.vault_path
        if not vault:
            CONSOLE.print("  No vault configured; /stale needs a .silica vault in a git repo.")
            return True
        show_all = "--all" in parts[1:]
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
            else:
                CONSOLE.print("  No stale docs — every documents: note matches its code_ref.")
            return True
        CONSOLE.print("  Run [bold]/nucleate <path>[/] to regenerate, or edit and re-badge.")
        return True

    if cmd == "/impact":
        from pathlib import Path
        from silica.kernel.code.codegraph import compute_impact
        vault = CONFIG.vault_path
        if not vault:
            CONSOLE.print("  No vault configured; /impact needs a vault inside a git repo.")
            return True
        range_spec = parts[1] if len(parts) > 1 else None
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

    if cmd == "/plans":
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

    if cmd == "/path":
        from silica.kernel.recall.mindmap import note_resolver, reading_path
        try:
            toks = shlex.split(raw_input.strip())[1:]  # honours quoted titles with spaces
        except ValueError:
            CONSOLE.print('  Unbalanced quotes. Usage: /path "<note A>" "<note B>"')
            return True
        endpoints = [t for t in toks if not t.startswith("-")]
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

    if cmd == "/contested":
        from silica.driver import DRIVER
        from silica.kernel.write.contested import CONTESTED_KEY, CONTRADICTIONS_KEY
        # ponytail: frontmatter scan of every note per call; index it if a
        # 10k-note vault ever makes this command feel slow.
        found: list[tuple[str, list[str]]] = []
        for ref in DRIVER.list_files(""):
            try:
                props = DRIVER.props_of(ref.path)
            except Exception:
                continue  # attachments / unreadable frontmatter — not contested
            if props and props.get(CONTESTED_KEY):
                contras = [str(c) for c in (props.get(CONTRADICTIONS_KEY) or [])]
                found.append((ref.path, contras))
        if not found:
            CONSOLE.print("  No contested notes — no unresolved contradictions.")
            return True
        CONSOLE.print(f"  {len(found)} contested note(s):")
        for note_path, contras in sorted(found):
            CONSOLE.print(f"  · [bold]{note_path}[/]")
            for c in contras:
                CONSOLE.print(f"      conflicts with: {c}")
        CONSOLE.print(
            "  Resolve by editing the note, then remove `contested: true` and its callout."
        )
        return True

    if cmd == "/episodes":
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
        # ponytail: `--save=` splits on whitespace like its sibling direct
        # commands, so a path with spaces needs a rename; shlex it if that bites.
        save = next((a[len("--save="):] for a in parts[1:] if a.startswith("--save=")), "")
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

    if cmd == "/undo":
        from silica.driver import DRIVER
        from silica.kernel.write.checkpoints import get_checkpoint_store

        store = get_checkpoint_store()
        note_path = parts[1] if len(parts) > 1 else store.most_recent_path()
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

    if cmd == "/revert":
        from silica.kernel.write.undo_journal import get_undo_journal, revert_run
        parts_split = raw_input.strip().split(maxsplit=1)
        vault = CONFIG.vault_path.strip() or None
        run_id = parts_split[1].strip() if len(parts_split) > 1 else get_undo_journal().last_active_run(vault=vault)
        if not run_id:
            CONSOLE.print("  Nothing to revert — no runs recorded for this vault.")
            return True
        res = revert_run(run_id)
        stale = len(res.get("stale", []))
        line = (
            f"  Revert {run_id[:8]}…: {len(res['reverted'])} writes reverted, "
            f"{len(res['skipped'])} skipped (modified), "
            f"{stale} stale (vault changed), {len(res['errors'])} errors."
        )
        CONSOLE.print(line)
        return True

    if cmd == "/review":
        from silica.kernel.recall.deferred import get_deferred_store
        store = get_deferred_store()
        flush_hash = next((p[len("--flush="):] for p in parts[1:] if p.startswith("--flush=")), None)
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

    if cmd == "/curate":
        apply = any(p == "--apply" for p in parts[1:])
        positional = [p for p in parts[1:] if not p.startswith("-")]
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

    if cmd == "/keep":
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

    return False


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
    /refine, /enrich and /dedup — the async, resumable, progress-tracked path."""
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
    return (
        f"A ledger for /{kind} has been created with {len(payloads)} chunk(s) "
        f"(run_id={run.run_id}). Use `silica_ledger_next` with this run_id to execute them."
    )


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
    folders = sorted({
        str(Path(r.path or r.name).parent)
        for r in DRIVER.list_files("")
        if (r.path or r.name).endswith(".md")
    } - {".", inbox})
    folders = [f for f in folders if not f.startswith(f"{inbox}/")]
    excerpt = (DRIVER.read_note(md_files[0]).content or "")[:1500]
    prompt = (
        "Pick the single most relevant destination folder for nucleating this "
        "content into a knowledge vault. Reply with ONLY the folder path on one "
        "line, no quotes. Prefer an existing folder; invent a sensible new path "
        "only if nothing fits.\n\n"
        "Existing folders:\n" + "\n".join(f"- {f}" for f in folders[:200]) +
        f"\n\nContent excerpt ({md_files[0]}):\n{excerpt}"
    )
    resp = call_llm(CONFIG.model, [{"role": "user", "content": prompt}], max_tokens=2048)
    lines = [ln.strip().strip('"').strip("`").rstrip("/") for ln in (resp.text or "").splitlines()]
    pick = next((ln for ln in lines if ln), "")
    if not pick:
        raise ValueError("empty folder pick")
    return pick


def _target_and_save(args: list[str]) -> tuple[str, str]:
    """Split `<free-text target words> [--save=<path>]` into (target, save_path)."""
    save_path = ""
    words: list[str] = []
    for arg in args:
        if arg.startswith("--save="):
            save_path = arg[len("--save="):]
        elif not arg.startswith("-"):
            words.append(arg)
    return " ".join(words).strip(), save_path


def _save_or_readonly_clause(save_path: str) -> str:
    """The trailing persistence contract shared by /schematize and /diagram."""
    if save_path:
        return (
            f"Then write it to the note at `{save_path}` using silica_write_note "
            f"(create it if missing, overwrite if present): the table/diagram is "
            f"the entire body, plus a one-line title."
        )
    return "READ-ONLY: do not create, edit, patch, or move any note."


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
    return question, (
        f"Answer this from the web, not from the vault: {question}\n"
        "Use `web_search` to find pages and `web_fetch` to read the ones that look "
        "like they answer it — a search snippet is not the article. Then answer in "
        "prose, and say plainly that the answer comes from the web rather than from "
        "the user's own notes. Do not write a Sources section: the citations are "
        "appended mechanically from the pages you actually opened."
    )


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
        # the same key never reaches min_runs, and the promotion queue stays
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
    keys = [a for a in args if not a.startswith("-")]
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
            # of run ids would need every chain re-walked for one console line.
            runs = max(m.run_count for m in members)
            since = min(m.since for m in members)
            CONSOLE.print(f"  · [bold]{ent}[/] — {runs} runs since {since}: {attrs}")
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


def _expand_workflow_shortcut(user_input: str) -> str | None:
    """Expand workflow shortcuts (e.g. /report, /nucleate) into agent-directed messages.

    Returns the expanded message string, or None if the input is not a
    recognised shortcut. Expanded messages flow through the normal agentic
    loop so the agent calls the tools and follows the steering protocol.

    Syntax:
        /report [folder] [--top-k=N] [--embeddings]
        /nucleate <file|folder...> [--target=DIR] [--hub=H] [--keep-sources]
        /convert <file...> [--target=DIR]
        /summarize <note|folder...>
        /explain "<concept>" [--level=intro|expert]
        /compare "<A>" "<B>" [...]
        /quiz [note|folder] [--n=10]
        /relate <note> [--n=8]
        /schematize <note|folder|topic> [--save=<path>]
        /diagram <note|folder|topic> [--save=<path>]

    Examples:
        /report
        /report Concepts/ML
        /report --top-k=15 --embeddings
        /report Inbox --embeddings
        /nucleate Inbox/notes.md --target=Concepts/AI
        /nucleate Inbox/notes.md
        /nucleate silica/cli.py
        /nucleate silica/kernel                 (folder → one stub per source file)
        /nucleate paper.pdf --target=Concepts/AI
        /nucleate "Inbox/papers/With Spaces.pdf" --target=Concepts/AI
        /convert paper.pdf
        /schematize "the ingest pipeline"
        /diagram Concepts/ML --save=Concepts/ML/map.md
    """
    if not user_input.strip().startswith("/"):
        return None  # not a shortcut — skip shlex entirely, plain prose can have stray quotes/apostrophes
    try:
        parts = shlex.split(user_input.strip())  # honours quoted paths with spaces
    except ValueError:
        return "Error: unbalanced quotes in command. Wrap paths with spaces in \"...\"."
    if not parts:
        return None

    cmd = parts[0].lower()

    if cmd == "/promote":
        return _promote(parts[1:])

    if cmd == "/nucleate":
        args = parts[1:]
        if not args:
            return _drain_wal()
        files: list[str] = []
        target_dir = ""
        hub = ""
        keep_sources = False
        for arg in args:
            if arg.startswith("--target="):
                target_dir = arg[len("--target="):]
            elif arg.startswith("--hub="):
                hub = arg[len("--hub="):]
            elif arg == "--keep-sources":
                keep_sources = True  # verbatim leaf in sources/ beside the notes
            elif not arg.startswith("-"):
                files.append(arg)  # preserve original case

        from pathlib import Path
        from silica.kernel.vault_manifest import get_active_manifest
        from silica.sources.convert import convert
        from silica.kernel.write.undo_journal import get_undo_journal
        from silica.sources.registry import adapter_for, expand_folder, folder_rel, stage
        from silica.tools.atomic import notes_under

        enabled = get_active_manifest().sources
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
                if group:
                    CONSOLE.print(f"  {f}: [bold]{len(group)}[/] note(s)")
            expanded.extend(group or [f])
        files = expanded

        md_files: list[str] = []
        staged = 0
        needs_agent = not files  # only flags given (dropped --folder=) → agent infers
        # One run per /nucleate invocation, so /revert undoes the batch the user
        # asked for rather than one file of it. A run with no inverses (nothing
        # staged) is invisible to last_active_run, so opening it costs nothing.
        undo_run = get_undo_journal().start_run(
            source="nucleate", vault=CONFIG.vault_path.strip() or None
        ) if files else None
        for f in files:
            adapter = adapter_for(f, enabled=enabled)
            if adapter is None:
                # No source claims this file type → try the converter fallback
                # (PDF today). The CONVERTED .md is what the FSM re-reads.
                try:
                    md_files.extend(convert(f, dest_dir=target_dir))
                except ValueError as e:
                    # A path with a real extension is a genuinely-unsupported file:
                    # skip it deterministically (no round-trip can convert a .csv).
                    # A bare name or folder (no suffix) is a resolvable intent the
                    # flag parser couldn't read — let the agent handle it.
                    if Path(f).suffix:
                        CONSOLE.print(f"  [yellow]Skipped {f}: {e}[/]")
                    else:
                        needs_agent = True
                continue
            result = stage(adapter, f, run_root.get(f, ""), undo_run)
            if result["status"] == "distill":
                md_files.append(f)
            elif result["status"] == "ok":
                staged += 1
                code_ref = result["meta"].get("code_ref", "")
                if len(files) <= 10:  # a whole subsystem would flood the terminal
                    CONSOLE.print(
                        f"  Wrote [bold]{result['note_path']}[/] "
                        f"(code_ref {code_ref[:8]})."
                    )
            else:
                CONSOLE.print(f"  [yellow]{f}: {result.get('message', '')}[/]")

        if staged:
            if len(files) > 10:
                CONSOLE.print(f"  Wrote [bold]{staged}[/] code note(s). /wiki for prose.")
            CONSOLE.print("  [dim]/revert undoes this run.[/]")

        if not md_files:
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

        from pathlib import Path as _Path
        from silica.kernel.write.provenance import check_renucleate, content_sha256

        for mf in md_files:
            try:
                incoming_sha = content_sha256(mf)
                if not incoming_sha:
                    continue
                modified, prior_notes = check_renucleate(_Path(mf).name, incoming_sha)
                if modified:
                    CONSOLE.print(
                        f"  [yellow]re-nucleate of a modified source: {prior_notes} note(s) "
                        f"derived from the previous version[/]"
                    )
            except Exception as exc:
                logger.debug("/nucleate: re-nucleate provenance check skipped for %s (non-fatal): %s", mf, exc)

        if not target_dir:
            # auto-target: one small folder-pick call, not a full agent turn;
            # per-note thematic placement stays /organize's job.
            try:
                target_dir = _pick_target_folder(md_files)
                CONSOLE.print(f"  auto-target: [bold]{target_dir}[/]")
            except Exception as exc:
                logger.debug("/nucleate: auto-target pick failed (non-fatal): %s", exc)

        if not target_dir:
            # Fallback: hand the folder choice to the agent (legacy behavior).
            files_json = json.dumps(md_files)
            msg = (
                f"Run the Injector pipeline for {len(md_files)} file(s).\n"
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

        # Direct FSM dispatch — no LLM orchestrator. The old path round-tripped
        # the whole session history through the model on every turn just to
        # relay these arguments to silica_run_injector (~40% of a nucleate
        # run's tokens for a handful of decision tokens).
        from silica.router.coordinator import Coordinator

        CONSOLE.print(f"  nucleate: {len(md_files)} file(s) → [bold]{target_dir}[/]")
        try:
            result = Coordinator(
                inbox_files=md_files, target_dir=target_dir, hub=hub or None,
                keep_sources=keep_sources,
            ).run()
        except ValueError as exc:
            # A path outside the vault (or any other rejected argument) is user
            # error, not a crash: the REPL keeps the session.
            CONSOLE.print(f"  [yellow]nucleate: {exc}[/]")
            return ""
        status = result.get("final_status") or result.get("error") or "done"
        failed = result.get("failed_chunks") or []
        extra = f" — {len(failed)} chunk(s) failed" if failed else ""
        CONSOLE.print(f"  nucleate finished: [bold]{status}[/]{extra} — details in log.md")
        return ""

    if cmd == "/settings":
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
        args = parts[1:]
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

    if cmd == "/convert":
        args = parts[1:]
        files = [a for a in args if not a.startswith("-")]
        target_dir = next((a[len("--target="):] for a in args if a.startswith("--target=")), "")
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

    if cmd == "/web-search":
        from rich.markup import escape

        from silica.sources.web_research import web_research, _DEFAULT_MAX_SEARCHES
        from silica.ui.renderer import make_progress_callback
        args = parts[1:]
        max_searches = _int_flag(args, "--max-searches=", _DEFAULT_MAX_SEARCHES)
        concept = " ".join(a for a in args if not a.startswith("-")).strip()
        if not concept:
            return 'Error: /web-search requires a concept. Usage: /web-search "<concept>" [--max-searches=N]'

        try:
            note_rel = web_research(
                concept, max_searches=max_searches,
                tool_progress_callback=make_progress_callback(),
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
        return ""  # fully handled inline — sentinel: nothing for the agent

    if cmd == "/fetch":
        from rich.markup import escape

        from silica.sources.web_research import fetch_to_inbox
        url = " ".join(parts[1:]).strip()
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

    if cmd == "/report":
        args = parts[1:]
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
            f"with_cooccurrence={'true' if with_cooccurrence else 'false'}, seed_ledger=true.\n"
            f"Then STOP. Write a short, human-readable brief in chat from the returned `digest` "
            f"(totals, top hubs, and how many fixes are available: auto / propose / issues), and "
            f"point the user to the GRAPH_REPORT.md that was written.\n"
            f"Do NOT run the steering loop, do NOT call `silica_ledger_next`, and do NOT apply any "
            f"autolinks, corrections, renames, or deletions. Instead, ask the user whether they want "
            f"to apply the changes. Only if they explicitly say yes, resume the run (`run_id`) and "
            f"follow the steering loop."
        )

    if cmd in ("/refine", "/enrich"):
        from silica.driver import DRIVER
        from silica.tools.graph import _in_folder

        args = parts[1:]
        folder = next((p for p in args if not p.startswith("-")), "")

        # list_files(folder) pre-filters loosely (startswith); _in_folder tightens
        # it so /refine Foo never leaks into a sibling FooBar/ folder.
        paths = [r.path for r in DRIVER.list_files(folder=folder) if _in_folder(r.path, folder)]
        if not paths:
            return f"Error: no files found in '{folder}'."

        cap = "silica_refine_batch" if cmd == "/refine" else "silica_enrich_batch"
        payloads = [{"note_paths": chunk} for chunk in _chunk_by_json_size(paths)]
        return _seed_batch_ledger(cap, payloads, kind=cmd.strip("/"), label=folder or "vault")

    if cmd == "/dedup":
        from silica.tools.runners import _scan_dedup_pairs

        args = parts[1:]
        folder = " ".join(a for a in args if not a.startswith("-"))

        pairs, err = _scan_dedup_pairs(folder)
        if err:
            CONSOLE.print(f"  [yellow]{err}[/]")
            return ""  # handled inline — nothing for the agent
        if not pairs:
            CONSOLE.print(f"  No near-duplicate pairs in [bold]{folder or '(vault)'}[/].")
            return ""

        payloads = [{"pairs": chunk} for chunk in _chunk_by_json_size(pairs)]
        return _seed_batch_ledger("silica_dedup_pairs", payloads, kind="dedup", label=folder or "vault")

    if cmd == "/organize":
        args = parts[1:]
        intent_parts: list[str] = []
        scope = ""
        taxonomy_file = ""
        apply_now = False
        merge = False
        move_uncat = False

        i = 0
        while i < len(args):
            arg = args[i]
            if arg.startswith("--scope="):
                scope = arg[len("--scope="):]
            elif arg.startswith("--file="):
                taxonomy_file = arg[len("--file="):]
            elif arg in ("--apply",):
                apply_now = True
            elif arg in ("--merge",):
                merge = True
            elif arg in ("--move-uncategorized",):
                move_uncat = True
            elif not arg.startswith("-"):
                intent_parts.append(arg)
            i += 1

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

    # --- reader commands: agent-directed, strictly read-only ---------------

    if cmd == "/summarize":
        targets = [a for a in parts[1:] if not a.startswith("-")]
        if not targets:
            return "Error: /summarize requires a note or folder. Usage: /summarize <note|folder...>"
        listing = ", ".join(f"`{t}`" for t in targets)
        return (
            f"Summarize {listing} from the vault.\n"
            f"Resolve each target (note path, note title, or folder — list a folder's notes and "
            f"read them). Then write a digest in chat: lead with the core ideas, use tables for "
            f"anything enumerable (comparisons, parameters, timelines), keep it scannable.\n"
            f"READ-ONLY: do not create, edit, patch, or move any note."
        )

    if cmd == "/explain":
        level = ""
        words: list[str] = []
        for arg in parts[1:]:
            if arg.startswith("--level="):
                level = arg[len("--level="):]
            elif not arg.startswith("-"):
                words.append(arg)
        concept = " ".join(words).strip()
        if not concept:
            return 'Error: /explain requires a concept. Usage: /explain "<concept>" [--level=intro|expert]'
        register = {
            "intro": "for a newcomer: plain language, concrete analogies, no unexplained jargon",
            "expert": "for an expert: precise and technical, no hand-holding",
        }.get(level, "for a practitioner: clear, correct, minimal jargon")
        # The attribution clause is a measured defect of this command, not a guess:
        # evals/probe_explain_spans.py (2026-07-26, 398 claims) found ~4.6% of claims
        # attributed to a named note that does not support them, and as many drawn
        # from general knowledge with no note at all.
        return (
            f"Explain {json.dumps(concept)} grounded in this vault, {register}.\n"
            f"Search the vault (semantic search + related notes), read the top matches, and explain "
            f"the concept in chat, citing every note you drew on as a [[wikilink]]. If the vault has "
            f"nothing relevant, say so plainly: do not silently answer from general knowledge alone.\n"
            f"Attribute a claim to a note only if that note states it. A note that merely sits near "
            f"the topic is not a source for it, and a point no note supports goes in its own "
            f"sentence, marked as not coming from the vault.\n"
            f"READ-ONLY: do not create, edit, patch, or move any note."
        )

    if cmd == "/compare":
        subjects = [a for a in parts[1:] if not a.startswith("-")]
        if len(subjects) < 2:
            return 'Error: /compare requires at least two subjects. Usage: /compare "<A>" "<B>"'
        listing = ", ".join(f"`{s}`" for s in subjects)
        return (
            f"Compare {listing} using the vault.\n"
            f"Each subject is a note (path or title) or a concept — locate and read the matching "
            f"note(s) for each. Output in chat: a comparison table (one column per subject, "
            f"dimensions as rows), then a short similarities/differences rundown. If any involved "
            f"note carries `contested: true`, or the notes contradict each other, call that out "
            f"explicitly. A contradiction the reader confirms is worth recording: offer to run "
            f"silica_flag_note on the note that is wrong, and only run it once they say so.\n"
            f"READ-ONLY apart from that flag: do not create, edit, patch, or move any note."
        )

    if cmd == "/quiz":
        n = _int_flag(parts[1:], "--n=", 10)
        targets = [a for a in parts[1:] if not a.startswith("-")]
        if targets:
            source = "from " + ", ".join(f"`{t}`" for t in targets)
            pick = "Read the note(s) (list a folder's notes first)."
        else:
            # No target: the review queue picks. Recall failures are the whole
            # point of logging them, so an untargeted /quiz spends its questions
            # where the reader has already been measured wrong.
            source = "from the notes this reader keeps getting wrong"
            pick = (
                "Call silica_weak_notes to pick the targets, and read them. If it comes back "
                "empty nothing has been graded yet: say so, then quiz the vault's recent or "
                "central notes instead."
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
            f"[[wikilink]], then call silica_record_quiz once with one entry per question "
            f"({{path, correct}}). Grade an unanswered or skipped question as incorrect.\n"
            f"A wrong answer is the reader's miss, not the note's fault, and needs nothing "
            f"beyond the grade. If grading instead exposes a fault in the note itself (it "
            f"states something wrong, or contradicts another note you read), offer to record "
            f"that with silica_flag_note, and only run it once the reader says so.\n"
            f"READ-ONLY apart from that flag: do not create, edit, patch, or move any note."
        )

    if cmd == "/relate":
        n = _int_flag(parts[1:], "--n=", 8)
        targets = [a for a in parts[1:] if not a.startswith("-")]
        if not targets:
            return "Error: /relate requires a note. Usage: /relate <note> [--n=8]"
        target = targets[0]
        return (
            f"Map how and why `{target}` relates to its most relevant neighbors in the vault.\n"
            f"Resolve the note, then pull its top {n} related notes via silica's relatedness "
            f"(the fusion of embeddings + co-occurrence). Read the target and each neighbor enough "
            f"to judge the link, and note which neighbors the target already [[wikilinks]].\n"
            f"Output in chat a Markdown table: | Neighbor | Relation | Why | Link |. "
            f"For Relation pick the type that fits — common ones: prerequisite, elaborates, "
            f"contradicts, sibling, example-of, depends-on, alternative-to. Why is one line grounded "
            f"in the notes. Link is [[the neighbor]] if already linked, else 'latent'. Cite every "
            f"neighbor as a [[wikilink]]. If a neighbor is `contested: true` or contradicts the "
            f"target, say so in the Why column, and offer to record the contradiction with "
            f"silica_flag_note; only run it once the reader says so.\n"
            f"READ-ONLY apart from that flag: do not create, edit, patch, or move any note."
        )

    if cmd == "/schematize":
        target, save_path = _target_and_save(parts[1:])
        if not target:
            return "Error: /schematize requires a target. Usage: /schematize <note|folder|topic> [--save=<path>]"
        return (
            f"Schematize {json.dumps(target)} from the vault.\n"
            f"Resolve the target: it may be a note (path or title), a folder (list and "
            f"skim its notes), or a general topic (search the vault, then read the top "
            f"matches).\n"
            f"Output in chat: a one-line caption, then a Markdown table whose rows/columns "
            f"best decompose what you found (components, phases, comparison dimensions, "
            f"whatever shape fits); do not force a fixed template.\n"
            f"{_save_or_readonly_clause(save_path)}"
        )

    if cmd == "/diagram":
        target, save_path = _target_and_save(parts[1:])
        if not target:
            return "Error: /diagram requires a target. Usage: /diagram <note|folder|topic> [--save=<path>]"
        return (
            f"Diagram {json.dumps(target)} from the vault.\n"
            f"Resolve the target the same way as /schematize (note, folder, or topic; "
            f"search and read as needed).\n"
            f"Pick whichever Mermaid diagram type fits what you found best: flowchart/graph "
            f"for architectures and processes, mindmap for concept trees, sequence for "
            f"temporal flows, classDiagram or erDiagram for structured relationships, "
            f"timeline for chronologies. Do not default to one type mechanically.\n"
            f"Output in chat: a one-line caption, then a single fenced ```mermaid block "
            f"and nothing else.\n"
            f"{_save_or_readonly_clause(save_path)}"
        )

    return None


def _handle_slash_command(cmd: str, messages: list[dict]) -> bool | None:
    """Handle a meta slash command. True = handled, False = exit the REPL,
    None = not a recognized command (the caller hands it to the agent)."""
    cmd = cmd.strip().lower()

    if cmd in ("/exit", "/quit", "/q"):
        return False  # Signal to exit

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
    the model actually answers (right key, model id valid, endpoint serving) —
    run_checks only probes key-presence and /models reachability, never a paid
    call. Returns True on a non-empty reply, False otherwise; prints the outcome."""
    if not _model_configured():
        CONSOLE.print("  [yellow]⚠ live probe skipped — no model configured[/]")
        return True
    from silica.agent.llm import call_llm
    CONSOLE.print(f"  [dim]→ live probe: asking {CONFIG.model} to reply…[/]")
    try:
        resp = call_llm(CONFIG.model, [{"role": "user", "content": "Reply with: ok"}], max_tokens=5)
    except Exception as e:  # any provider/transport error → not working
        CONSOLE.print(f"  [red]✗ live probe failed:[/] {e}")
        return False
    if (resp.text or "").strip():
        CONSOLE.print("  [green]✓ live probe: model replied[/]")
        return True
    CONSOLE.print("  [red]✗ live probe: empty reply[/]")
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


def _dispatch_subcommand(args: list[str]) -> int | None:
    """Handle `silica doctor` / `init` / `setup` / `connect` / `mcp` / `update`.

    Returns an exit code, or None when no subcommand matched (→ REPL).
    Lazy imports keep REPL startup unchanged. Module attributes (not `from`
    imports) so tests can monkeypatch run_checks / run_wizard / run_connect.
    """
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
    if args[:1] == ["import"]:
        import silica.capture as capture_mod
        target = next((a for a in args[1:] if not a.startswith("-")), "")
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
        _ensure_servers()  # report the state after the autostart, not before it
        results = checks.run_checks(CONFIG)
        checks.render_report(results)
        # --live: opt-in real completion (costs a token on hosted providers).
        live_ok = _doctor_live_probe() if "--live" in args[1:] else True
        return 1 if (checks.has_failures(results) or not live_ok) else 0
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
        with contextlib.redirect_stdout(sys.stderr):
            _activate_repo_mode()
            from silica.kernel.vault_manifest import apply_manifest_to_config
            apply_manifest_to_config()
            _ensure_servers()
        logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
        import silica.ui.mcp as mcp_mod
        return mcp_mod.run_mcp(all_tools="--all" in args[1:])
    return None


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
        except ImportError:
            CONSOLE.print("  [red]The GUI needs an extra:[/] pip install 'silica\\[gui]'")
            sys.exit(1)
        serve(port=_gui_port())
        return

    # Obsidian bridge: host the rpc channel so the plugin can dial in and the
    # driver hot-swaps to ws (writes land through Obsidian's vault API while
    # the app is open). Silent no-op without [connect] or .obsidian/.
    from silica.ui.connect import start_bridge_thread
    _bridge = start_bridge_thread()

    # Wizard first: it prints its own banner and re-execs on success, so running
    # it after print_home() showed the banner twice in one screen.
    _autolaunch_wizard_if_unconfigured()  # re-execs on success; returns otherwise
    print_home()
    if _bridge is not None:
        CONSOLE.print(f"  [dim]Obsidian bridge on ws://127.0.0.1:{_bridge.port}[/]\n")
    if not _model_configured():
        CONSOLE.print(_NO_MODEL_HINT)

    session = build_session()
    messages = _fresh_messages()
    collapsed: set[int] = set()  # message indices already elided by compaction
    # This session's own identity, for the capture lane. Random, not a clock:
    # two silica processes started in the same second share the same vault WAL,
    # and a deterministic name would have one overwrite the other's envelope.
    # capture_session is opt-in and fail-open in itself, so both call sites
    # below are the bare call — no wrapper, nothing to guard.
    from silica.capture import capture_session
    session_id = uuid.uuid4().hex[:12]
    incognito = False

    from silica.ui.renderer import make_progress_callback
    callback = make_progress_callback()

    while True:
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
            expanded = _expand_workflow_shortcut(user_input)
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
            if cmd == "/incognito":
                incognito = not incognito
                CONSOLE.print(
                    "  [dim]incognito: this session will not be captured[/]"
                    if incognito else "  [dim]incognito off: capture resumed[/]"
                )
                continue

            if cmd == "/clear":
                # Before the wipe: /clear destroys this conversation, so the
                # session's own end envelope will not contain it.
                if not incognito:
                    capture_session(messages, session_id=session_id, driver="tui",
                                    event="session_clear")
                CONSOLE.clear()
                print_home()
                messages[:] = _fresh_messages()
                collapsed = set()  # indices reset with the history
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
                    web_turn_constraints() if web else AgentConstraints(tools=chat_tools())
                ),
            )
            if web:
                answer = watch.attribute(answer, messages)
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
            CONSOLE.print("\n  [dim](interrupted)[/]")
        except Exception as e:
            callback.close()
            logger.exception("Agent error")
            CONSOLE.print(f"\n  [bold red]Error:[/] {e}\n")

    # Every exit from the REPL passes here: /exit, Ctrl+D, Ctrl+C at the prompt.
    if not incognito:
        capture_session(messages, session_id=session_id, driver="tui")


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
