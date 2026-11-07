# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The system context a conversation starts from, for every surface."""
from __future__ import annotations

import logging

from silica.config import CONFIG
from silica.prompts import _lang_rule, system_prompt
from silica.session import meter

logger = logging.getLogger(__name__)


def inject_vault_map(messages: list[dict]) -> None:
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


def today_line() -> str:
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


def vault_scope() -> str:
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


def seed_messages(math: bool = False, app: str = "") -> list[dict]:
    """The system context a fresh conversation starts from: prompt, date, vault
    scope, vault map, and the restated language rule (`reply_language`).

    Shared by the TUI and the GUI (`ui/web/session.build_seed`, which passes
    math=True for the MathML renderer and in `app` the help panel the chat
    answers from, ADR-0115). It used to be TUI-only, and the GUI's
    own seed had drifted to prompt + map: no `vault_scope`, so the model was
    blind to `write_dir`, and no closing language line, which is the fix commit
    93b6d2d shipped here and never carried across. One builder, no drift.
    """
    reply = reply_language()
    messages: list[dict] = [{"role": "system", "content": system_prompt(reply, math=math)}]
    messages.append({"role": "system", "content": today_line()})
    messages.append({"role": "system", "content": vault_scope()})
    if app:
        # Before the map, which is the seed's bulk. On 2026-10-16 the seed with
        # it answered 13 to 15 of 14 to 15 how-to questions a run, against 4 to 5
        # without (docs evaluation/app-knowledge-2026-10-16).
        messages.append({"role": "system", "content": app})
    inject_vault_map(messages)
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


def reply_language() -> str:
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


def fresh_messages() -> list[dict]:
    """`seed_messages` plus the live context meter — session start and /clear.

    The meter update is what the GUI must NOT do off the request path (it would
    clobber the meter of the conversation in progress), so it lives here rather
    than in the shared builder.
    """
    messages = seed_messages()
    meter.update_context_tokens(messages)
    return messages
