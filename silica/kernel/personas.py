# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Personas (ADR-0127 point 3): a skill whose frontmatter carries
`metadata: {persona: true}`, which a conversation wears instead of running once.

A persona is a skill so that everything a skill already has serves it: the
three roots and their shadowing, the install preview in Settings › Skills, the
`/` picker. What differs is the turn: `/<name>` on a persona writes its body
into the conversation's own system line, the way a workspace's instructions
are written (server._apply_workspace), and every later answer reads it.
`/persona` lists them and names the one on, `/persona off` takes it off.

The flag sits under `metadata`, the map Agent Skills leaves to each harness:
another harness reads the same file as an ordinary skill that runs once.
silica-core's loader drops `metadata`, so the flag is read here.
"""
from __future__ import annotations

from pathlib import Path

ORIGIN = "persona"
COMMAND = "/persona"   # answered by `command` below, ahead of both REPL tables
# On the persona's system line and on every user turn it ran under, so a
# reopened chat puts it back (server.load_session) and a reader of the
# narration can tell which answers it shaped.
TURN_KEY = "silica_persona"
# Every call of every turn pays the line, so it is capped: 2,000 characters is
# about 500 to 1,000 tokens, 7 to 14% of the 6,947-token chat prefix
# (tests/test_token_meter.py). The longest of the three files a lawyer sent
# on 2026-10-22 is 2,199 bytes with its frontmatter. Reopen with a persona that
# needs more and a measurement of what the extra length buys.
CAP = 2000

OFF_LINE = ("No persona applies to this conversation any more: answer as Silica "
            "normally does.")


def is_persona(skill) -> bool:
    from silica_core.kernel.write.frontmatter import split

    try:
        data, _raw, _body = split(Path(skill.path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return False   # the loader logged it already; an unreadable file is no persona
    meta = (data or {}).get("metadata")
    return isinstance(meta, dict) and meta.get("persona") is True


def available(vault=None) -> dict:
    """The persona skills this vault reaches, by name, nearest root winning."""
    from silica_core.kernel.skills import discover

    return {name: s for name, s in discover(vault).items() if is_persona(s)}


def line(skill) -> str:
    """The system line a conversation wearing `skill` carries."""
    from silica_core.kernel.skills import _COMMENT

    body = _COMMENT.sub("", skill.body).strip()
    if len(body) > CAP:
        body = body[:CAP].rsplit("\n", 1)[0]
    return (f'The person chose the persona "{skill.name}" for this conversation. Apply it to '
            "every answer, below Silica's own rules: it changes how you answer, never what the "
            "notes say or what you may write. Its instructions, in its author's words:\n\n" + body)


def active(messages: list[dict]) -> str:
    """The name of the persona this conversation wears, "" for none."""
    return next((str(m.get(TURN_KEY) or "") for m in messages if m.get("origin") == ORIGIN), "")


def apply(messages: list[dict], skill, can_insert: bool = True) -> bool:
    """Put `skill` on the conversation (None takes the persona off). Written
    once and edited in place, never removed, as the workspace line is: an
    insert shifts every later message, so a caller whose compaction recorded
    indices passes `can_insert=False` and gets False when there is no line yet.
    Before the seed's last system message, the language rule, which must stay
    last."""
    i = next((n for n, m in enumerate(messages) if m.get("origin") == ORIGIN), None)
    if skill is None:
        if i is not None:
            messages[i]["content"] = OFF_LINE
            messages[i][TURN_KEY] = ""
        return True
    if i is not None:
        messages[i]["content"] = line(skill)
        messages[i][TURN_KEY] = skill.name
        return True
    if not can_insert:
        return False
    head = next((n for n, m in enumerate(messages) if m.get("role") != "system"), len(messages))
    messages.insert(max(head - 1, 0), {"role": "system", "content": line(skill),
                                       "origin": ORIGIN, TURN_KEY: skill.name})
    return True


def command(text: str, messages: list[dict], vault=None, can_insert: bool = True) -> tuple[str, str] | None:
    """`/persona [name|off]` or `/<persona name> [text]`, carried out on
    `messages`: (what to tell the person, the text to send on as this turn).
    None when `text` is no persona command, so the caller dispatches it as
    before."""
    if not text.startswith("/"):
        return None
    head, _, rest = text.strip().partition(" ")
    head, rest = head[1:].lower(), rest.strip()
    found = available(vault)
    if "/" + head == COMMAND:
        want = rest.lower()
        if not want:
            names = ", ".join(f"/{n}" for n in found) or "none installed (Settings › Skills)"
            on = active(messages)
            return (f"Persona: {on or 'none'}. Available: {names}. "
                    "`/persona off` takes it off.", "")
        if want in ("off", "none"):
            apply(messages, None)
            return "Persona off: answers are Silica's own again.", ""
        head, rest = want, ""
        if head not in found:
            return f"No persona named {want!r}. `/persona` lists them.", ""
    if head not in found:
        return None
    skill = found[head]
    if not apply(messages, skill, can_insert):
        return ("This conversation has been compacted, so a persona cannot be added to it. "
                f"Open a new chat and type /{skill.name} there.", "")
    return f"Persona {skill.name} on for this conversation. `/persona off` takes it off.", rest
