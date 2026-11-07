# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

from __future__ import annotations

from silica.command_table import COMMANDS, Command


def skill_commands() -> tuple[Command, ...]:
    """The skills this vault reaches (ADR-0063), as workflow commands.

    A name a row of the table already answers is left out: dispatch tries those
    first, so the skill would be listed and never run. A skill from the vault
    or the home dir says so in its summary, since it can shadow a built-in and
    a vault is not always its reader's own.
    """
    from silica.config import CONFIG
    from silica.kernel.personas import is_persona
    from silica_core.kernel.skills import discover

    taken = {c.name for c in COMMANDS}
    out = []
    for s in discover(CONFIG.vault_path or None).values():
        if "/" + s.name in taken:
            continue
        summary = (s.description.splitlines() or [""])[0]
        # A persona is worn, not run (ADR-0127): the picker says which it is.
        kind = "persona" if is_persona(s) else "skill"
        if s.source != "builtin" or kind == "persona":
            where = "" if s.source == "builtin" else f"{s.source} "
            summary = f"{summary} ({where}{kind})" if summary else f"({where}{kind})"
        out.append(Command(name="/" + s.name, group="workflow", usage=s.argument_hint, summary=summary))
    return tuple(out)


def commands() -> tuple[Command, ...]:
    """Every command a turn can dispatch: the Python ones, then the skills."""
    return COMMANDS + skill_commands()


def command_names() -> tuple[str, ...]:
    return tuple(c.name for c in commands())


def unrecorded(line: str) -> bool:
    """Whether a typed line is one a command marks as able to carry a secret
    (`secret_verbs`), so no history may keep it. Matched the way dispatch
    reads a line: the name case-folded, then the first word after it."""
    parts = line.split(maxsplit=2)
    if len(parts) < 2:
        return False
    name, verb = parts[0].lower(), parts[1].lower()
    return any(c.name == name and verb in c.secret_verbs for c in COMMANDS)


def render_help() -> None:
    from rich.padding import Padding

    from silica.term.console import CONSOLE
    from silica.term.style import GROUP_STYLE, command_table

    CONSOLE.print()
    CONSOLE.print("  [bold]silica commands[/]")
    CONSOLE.print()

    every = commands()
    workflow = [c for c in every if c.group == "workflow"]
    direct = [c for c in every if c.group == "direct"]
    system = [c for c in every if c.group == "system"]

    CONSOLE.print(f"  [bold {GROUP_STYLE['workflow']}]Workflow[/]  [dim]· agent-directed[/]")
    CONSOLE.print(Padding(command_table(workflow, name_style=f"bold {GROUP_STYLE['workflow']}"), (0, 0, 0, 4)))
    CONSOLE.print()
    CONSOLE.print()

    CONSOLE.print(f"  [bold {GROUP_STYLE['direct']}]Direct[/]  [dim]· immediate, no LLM[/]")
    CONSOLE.print(Padding(command_table(direct, name_style=f"bold {GROUP_STYLE['direct']}"), (0, 0, 0, 4)))
    CONSOLE.print()
    CONSOLE.print()

    sys_line = "  ·  ".join(c.name for c in system)
    CONSOLE.print(f"  [bold {GROUP_STYLE['system']}]System[/]")
    CONSOLE.print(f"    [dim]{sys_line}[/]")
    CONSOLE.print()
