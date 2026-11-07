# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""/remember, /memory, /episodes, /incognito: what is kept about the person and this vault."""
from __future__ import annotations

import shlex
from pathlib import Path

from silica.command_table import Call, Result
from silica.config import CONFIG
from silica.session.words import remember_text, str_flag


def remember(c: Call) -> Result:
    """/remember <text>: the user's words into this vault's memory with no
    model in between, so the taint gate does not apply (ADR-0076 §6): it is
    the way to save from a conversation that gate closed. A bare /remember
    asks the model to keep what was just said, and that save still passes the
    taint gate (ADR-0076 §5)."""
    raw_input, out = c.raw, c.out
    from rich.markup import escape

    from silica.kernel import user_memory

    text = remember_text(raw_input)
    if not text:
        from silica.tools.user_memory import REMEMBER_DIRECTIVE

        return out.to_model(
            f"{REMEMBER_DIRECTIVE} Save it with silica_remember (action save): one short "
            "self-contained line, scope user for what is true of the person in every vault, "
            "scope vault for the state of this work. If nothing in the conversation is clearly "
            "what they mean, ask which fact to keep instead of guessing. Reply with what you saved."
        )
    from silica.obs import narration

    saved = user_memory.save("vault", text, session=narration.NARRATOR.sid or "")
    if not saved["ok"]:
        out.print(f"  [yellow]{escape(saved['error'])}[/]")
        for entry in saved.get("entries") or ():
            out.print(f"  [dim]{escape(entry)}[/]")
        return out
    out.print(f"  {escape(user_memory.describe(saved))}  "
              f"[dim]({saved['chars']}/{saved['budget']} characters · /memory shows it all)[/]")
    return out


def memory(c: Call) -> Result:
    """/memory: both memory files with their paths and budgets: what
    Settings > Memory shows in the GUI, for a terminal that has no such page."""
    out = c.out
    from rich.markup import escape

    from silica.kernel import user_memory

    view = user_memory.view()
    if view["paused"]:
        out.print("  [yellow]memory is paused (SILICA_MEMORY=False): kept, not shown, not written[/]")
    elif view["incognito"]:
        out.print("  [yellow]incognito: memory is kept but not shown or written this session[/]")
    home = str(Path.home())
    for scope, title in (("user", "you, in every vault"), ("vault", "this vault")):
        s = view["scopes"][scope]
        if not s["available"]:
            out.print(f"  [bold]{title}[/]  [dim]no vault open[/]")
            continue
        where = s["path"].replace(home, "~", 1)
        out.print(f"  [bold]{title}[/]  [dim]{escape(where)} · "
                  f"{s['chars']}/{s['budget']} characters[/]")
        body = s["text"].rstrip("\n")
        out.print("\n".join(f"    {escape(line)}" for line in body.splitlines())
                  if body else "    [dim](empty)[/]")
    return out


def episodes(c: Call) -> Result:
    """/episodes [key] — what episodic memory has accumulated."""
    args, raw_input, out = c.args, c.raw, c.out
    from rich.markup import escape

    from silica.kernel.recall.episodic import EpisodicStore, FactHit, render

    store = EpisodicStore()
    heads = sorted(store.live_facts(), key=lambda f: f.key)
    if not heads:
        out.print("  No episodic memory yet — nothing has been captured.")
        return out
    body = "\n\n".join(
        f"## {h.key}\n" + render([FactHit(fact=h, score=1.0)], store=store)
        for h in heads
    )
    out.print(escape(body))
    # Re-split the raw line with shlex so `--save="a b/x.md"` honours the
    # quoted path; a malformed quote falls back to the whitespace split.
    # (shlex is imported at module scope — a local `import shlex` here
    # rebinds the name for the WHOLE function, and /path's use of it above
    # then dies with UnboundLocalError before this branch ever runs.)
    try:
        save_args = shlex.split(raw_input.strip())[1:]
    except ValueError:
        save_args = args
    save = str_flag(save_args, "--save")
    if save:
        from pathlib import Path

        target = Path(save).expanduser().resolve()
        # Empty until a vault is adopted, and Path("") resolves to the cwd:
        # no vault means nothing to fall inside, not "everything under here".
        raw_vault = (CONFIG.vault_path or "").strip()
        vault = Path(raw_vault).expanduser().resolve() if raw_vault else None
        if vault is not None and target.is_relative_to(vault):
            # The one door in stays the gate: an episodic render dropped
            # into the vault would be an unreviewed note that indexes,
            # links and gets recalled — the echo channel, by hand.
            out.print(
                "  [yellow]Not inside the vault.[/] Session memory becomes a "
                "note through [bold]/promote <key>[/]; save this render "
                "somewhere else."
            )
            return out
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"# Episodes\n\n{body}\n", encoding="utf-8")
        except OSError as e:
            # The REPL calls this handler outside any try: a directory, a
            # read-only mount or a bad name would end the session.
            out.print(f"  [yellow]save failed: {escape(str(e))}[/]")
            return out
        out.print(f"  Saved → [bold]{escape(str(target))}[/]")
    return out


def incognito(c: Call) -> Result:
    """/incognito: this session off the record: not captured, and user
    memory neither shown to the model nor written (ADR-0076 §8). One flag per
    process, which is the TUI's session; the GUI clears it on a new chat."""
    messages, out = c.messages, c.out
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
        out.print("  [dim]incognito: this session is not captured, and memory is "
                  "neither shown nor saved[/]")
    elif had_slot or not user_memory.snapshot():
        out.print("  [dim]incognito off: capture and memory resumed[/]")
    else:
        out.print("  [dim]incognito off: capture and saving resumed; "
                  "memory shows from the next chat[/]")
    return out
